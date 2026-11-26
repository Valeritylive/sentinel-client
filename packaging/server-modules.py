#!/usr/bin/env python3
"""Выводит все модули sentinel_*, которые нужны серверу (транзитивно по import).

build-server-deb.sh кладёт в пакет ровно этот список, поэтому новый модуль,
импортированный сервером, больше не может «потеряться» при упаковке.
"""
import ast
import os
import sys

ENTRY = ("sentinel_server", "sentinel_server_ctl", "sentinel_server_settings",
         "sentinel_server_tray", "sentinel_server_helper")


def closure(root, entry=ENTRY):
    seen, todo = set(), list(entry)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        with open(os.path.join(root, name + ".py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found = [node.module]
            else:
                continue
            for mod in found:
                mod = mod.split(".")[0]
                if os.path.exists(os.path.join(root, mod + ".py")):
                    todo.append(mod)
    return sorted(seen)


if __name__ == "__main__":
    print("\n".join(closure(sys.argv[1] if len(sys.argv) > 1 else ".")))
