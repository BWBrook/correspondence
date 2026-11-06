#!/usr/bin/env python3
"""Show or set the human participant's display name on the board.

Messages store the human as ``User``; this name is what the browser, the
agents' instructions and exports show. Restart the browser service and
reconnect agents' MCP connections afterwards so they pick it up.
"""
import argparse

from correspondence.store import Store

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("name", nargs="?", help="new display name; omit to show the current one")
args = parser.parse_args()
store = Store()
print(store.set_user_name(args.name) if args.name else store.user_name())
