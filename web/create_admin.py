#!/usr/bin/env python3
"""
Sherlock — First-run admin setup and user management CLI.

Usage:
  python create_admin.py                   # interactive first-admin setup
  python create_admin.py add <username>    # add any user interactively
  python create_admin.py reset <username>  # reset a user's password
  python create_admin.py list              # list all users
"""

import sys
import getpass

# Must run from web/ dir so imports resolve
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from models import init_db, SessionLocal, User
from auth import create_user, reset_password, ensure_admin_exists, hash_password


def _prompt(label: str, required: bool = True) -> str:
    while True:
        val = input(f"  {label}: ").strip()
        if val or not required:
            return val
        print("  (required)")


def _prompt_password(label: str = "Password") -> str:
    while True:
        p1 = getpass.getpass(f"  {label}: ")
        p2 = getpass.getpass(f"  Confirm: ")
        if p1 == p2 and p1:
            return p1
        print("  Passwords do not match or are empty. Try again.")


def cmd_first_admin(db):
    print("\n  No admin account exists yet.")
    print("  Create the first Sherlock admin account.\n")
    username = _prompt("Username")
    display  = _prompt("Display name (e.g. 'Jane Smith')", required=False)
    password = _prompt_password()
    try:
        user = create_user(db, username, password, display, role="admin")
        print(f"\n  ✓ Admin account '{user.username}' created.")
    except ValueError as e:
        print(f"\n  ✗ {e}")
        sys.exit(1)


def cmd_add(db, username=None):
    print()
    username = username or _prompt("Username")
    display  = _prompt("Display name", required=False)
    role     = input("  Role [user/admin] (default: user): ").strip() or "user"
    password = _prompt_password()
    try:
        user = create_user(db, username, password, display, role=role)
        print(f"\n  ✓ User '{user.username}' ({user.role}) created.")
    except ValueError as e:
        print(f"\n  ✗ {e}")
        sys.exit(1)


def cmd_reset(db, username=None):
    print()
    username = username or _prompt("Username")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        print(f"  ✗ User '{username}' not found.")
        sys.exit(1)
    password = _prompt_password("New password")
    reset_password(db, user.id, password)
    print(f"\n  ✓ Password reset for '{username}'.")


def cmd_list(db):
    users = db.query(User).order_by(User.created_at).all()
    if not users:
        print("\n  No users found.")
        return
    print(f"\n  {'USERNAME':<20} {'DISPLAY NAME':<24} {'ROLE':<8} {'ACTIVE'}")
    print("  " + "-" * 64)
    for u in users:
        active = "yes" if u.active else "no"
        print(f"  {u.username:<20} {(u.display_name or ''):<24} {u.role:<8} {active}")
    print()


def main():
    init_db()
    db = SessionLocal()

    args = sys.argv[1:]
    cmd = args[0] if args else None

    print("\n  Sherlock — User Management")
    print("  " + "=" * 36)

    try:
        if cmd == "add":
            cmd_add(db, args[1] if len(args) > 1 else None)
        elif cmd == "reset":
            cmd_reset(db, args[1] if len(args) > 1 else None)
        elif cmd == "list":
            cmd_list(db)
        elif cmd is None:
            # Default: first-admin setup if none exists, else show help
            if not ensure_admin_exists(db):
                cmd_first_admin(db)
            else:
                print("\n  Admin already exists. Commands:\n")
                print("    python create_admin.py list              List all users")
                print("    python create_admin.py add <username>    Add a user")
                print("    python create_admin.py reset <username>  Reset password\n")
        else:
            print(f"\n  Unknown command: {cmd}")
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
