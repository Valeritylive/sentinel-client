import sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import sentinel_cluster as cluster
import sentinel_engine as engine_mod
import sentinel_remote as remote
import sentinel_security as security
import sentinel_server as server_mod
import sentinel_storage as storage
from fake_hikconnect import FakeSecrets

KEY = "k" * 64


class _Db(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        storage.add_group("Склад")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def config(role, **extra):
    return dict({"role": role, "peer_url": "https://peer:8765", "key": KEY, "heartbeat": 5, "failover": 30,
                 "sync": 5, "node": role}, **extra)


class FakePeer:
    def __init__(self):
        self.up = True
        self.state = {"active": True, "promoted": False, "version": "v1"}
        self.snapshot = None
        self.calls = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        if not self.up:
            raise OSError("connection refused")
        if path == "/cluster/heartbeat":
            return 200, dict(self.state), {}
        if path == "/cluster/snapshot":
            data, digest = self.snapshot
            return 200, data, {"X-Snapshot-SHA256": digest, "X-Snapshot-Version": self.state["version"]}
        if path == "/cluster/demote":
            self.state.update(active=False, promoted=False)
            return 200, {"ok": True}, {}
        return 404, {}, {}


class SignAndConfig(_Db):
    def test_sign_verify(self):
        headers = cluster.signed_headers(KEY, "GET", "/api/v1/cluster/heartbeat", now=1000)
        self.assertTrue(cluster.verify(KEY, "GET", "/api/v1/cluster/heartbeat", headers, now=1010))
        self.assertFalse(cluster.verify(KEY, "GET", "/api/v1/cluster/heartbeat", headers, now=1100))  # просрочено
        self.assertFalse(cluster.verify(KEY, "POST", "/api/v1/cluster/heartbeat", headers, now=1000))
        self.assertFalse(cluster.verify("x" * 64, "GET", "/api/v1/cluster/heartbeat", headers, now=1000))
        self.assertFalse(cluster.verify("", "GET", "/", {}))

    def test_config_validation_and_file_mode(self):
        with self.assertRaises(cluster.ClusterError):
            cluster.save_config(config("primary", key="short"))
        with self.assertRaises(cluster.ClusterError):
            cluster.save_config(config("standby", peer_url=""))
        with self.assertRaises(cluster.ClusterError):
            cluster.save_config(config("standby", failover=10))
        cluster.save_config(config("standby"))
        self.assertEqual(cluster.load_config()["role"], "standby")
        self.assertEqual(cluster.config_path().stat().st_mode & 0o777, 0o600)

    def test_snapshot_roundtrip_and_corruption(self):
        (storage.DATA / "secrets.json").write_text('{"h|80": "x"}')
        data, digest = cluster.make_snapshot()
        storage.add_group("Офис")
        (storage.DATA / "secrets.json").write_text("{}")
        cluster.restore_snapshot(data, digest)
        self.assertEqual([g["name"] for g in storage.load_groups()], ["Склад"])
        self.assertIn("h|80", (storage.DATA / "secrets.json").read_text())
        with self.assertRaises(cluster.ClusterError):
            cluster.restore_snapshot(data, "0" * 64)
        with self.assertRaises(cluster.ClusterError):
            cluster.restore_snapshot(b"not a zip")


class NodeLogic(_Db):
    def make(self, role, peer, clock, **extra):
        events = []
        node = cluster.ClusterNode(config(role, **extra), peer, lambda: events.append("up"),
                                   lambda: events.append("down"), clock)
        return node, events

    def test_standby_syncs_then_takes_over(self):
        peer, clock = FakePeer(), Clock()
        peer.snapshot = cluster.make_snapshot()
        node, events = self.make("standby", peer, clock)
        node.start(background=False)
        self.assertFalse(node.active)
        node.tick()
        self.assertEqual(node.synced_version, "v1")          # первая синхронизация
        node.tick()
        self.assertEqual(sum(1 for c in peer.calls if c[1] == "/cluster/snapshot"), 1)   # версия не менялась
        peer.up = False
        clock.now += 20
        node.tick()
        self.assertFalse(node.active)
        clock.now += 15
        node.tick()
        self.assertTrue(node.active and node.promoted)
        self.assertEqual(events, ["up"])
        self.assertTrue(node.heartbeat()["promoted"])

    def test_witness_blocks_takeover(self):
        peer, clock = FakePeer(), Clock()
        peer.up = False
        node, events = self.make("standby", peer, clock, witness="127.0.0.1:1")
        node.start(background=False)
        clock.now += 60
        with patch.object(cluster, "witness_ok", return_value=False):
            node.tick()
        self.assertFalse(node.active)
        self.assertIn("контрольный узел", node.peer_error)
        with patch.object(cluster, "witness_ok", return_value=True):
            node.tick()
        self.assertTrue(node.active)

    def test_primary_yields_and_failback(self):
        peer, clock = FakePeer(), Clock()
        peer.state.update(active=False)
        node, events = self.make("primary", peer, clock)
        node.start(background=False)
        self.assertTrue(node.active)
        peer.state.update(active=True, promoted=True, version="v2")   # резерв взял роль при разделении сети
        node.tick()
        self.assertFalse(node.active)
        self.assertTrue(node.yielded)
        peer.snapshot = cluster.make_snapshot()
        node.failback()
        self.assertTrue(node.active)
        self.assertFalse(peer.state["active"])
        self.assertEqual(events, ["up", "down", "up"])
        self.assertIn(("POST", "/cluster/demote"), peer.calls)

    def test_primary_starts_passive_when_standby_active(self):
        peer, clock = FakePeer(), Clock()
        peer.state.update(active=True, promoted=True)
        node, events = self.make("primary", peer, clock)
        node.start(background=False)
        self.assertFalse(node.active)
        self.assertTrue(node.yielded)
        self.assertEqual(events, [])

    def test_manual_operations_check_role(self):
        node, _events = self.make("primary", FakePeer(), Clock())
        with self.assertRaises(cluster.ClusterError):
            node.promote()
        standby, _ = self.make("standby", FakePeer(), Clock())
        with self.assertRaises(cluster.ClusterError):
            standby.failback()
        standby.promote()
        self.assertTrue(standby.active)
        standby.demote()
        self.assertFalse(standby.active)


class ServerCluster(_Db):
    def setUp(self):
        super().setUp()
        p = patch.object(storage, "_secret_tool", FakeSecrets()); p.start(); self.addCleanup(p.stop)
        security.ensure_admin("Adm1n-pass!")
        self.engines = []

        def new_engine():
            engine = engine_mod.Engine(receive_events=False, recording_enabled=False)
            self.engines.append(engine)
            return engine

        self.server = server_mod.make_server("127.0.0.1", 0, new_engine())
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.peer = FakePeer()
        self.peer.up = False
        self.node = server_mod.attach_cluster(self.server, config("standby", peer_url=self.url), new_engine,
                                              transport=self.peer, background=False)
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        for engine in self.engines:
            engine.stop()

    def test_passive_rejects_clients_then_promote(self):
        client = remote.ServerClient(self.url)
        health = client.health()
        self.assertFalse(health["active"])
        self.assertEqual(health["cluster"]["role"], "standby")
        client.login("admin", "Adm1n-pass!")        # вход разрешён, чтобы администратор видел состояние
        with self.assertRaises(remote.RemoteError) as caught:
            client.devices()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(client.request("GET", "/cluster/status")["role"], "standby")
        client.request("POST", "/cluster/promote", {})
        self.assertTrue(self.node.active)
        self.assertTrue(self.server.engine._threads)   # службы запущены
        self.assertEqual(client.devices(), [])
        self.assertTrue(client.health()["active"])

    def test_signed_peer_endpoints(self):
        self.node.promote()
        transport = cluster.http_transport(cluster.validate(config("primary", peer_url=self.url)))
        status, payload, _ = transport("GET", "/cluster/heartbeat")
        self.assertEqual((status, payload["active"], payload["promoted"]), (200, True, True))
        status, data, headers = transport("GET", "/cluster/snapshot")
        self.assertEqual(status, 200)
        cluster.restore_snapshot(data, headers["X-Snapshot-SHA256"])
        bad = cluster.http_transport(cluster.validate(config("primary", peer_url=self.url, key="z" * 64)))
        self.assertEqual(bad("GET", "/cluster/heartbeat")[0], 403)
        status, _payload, _ = transport("POST", "/cluster/demote")
        self.assertEqual(status, 200)
        self.assertFalse(self.node.active)


if __name__ == "__main__":
    unittest.main()
