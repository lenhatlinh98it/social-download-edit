"""Seller-only license key generator (command line). The owner edition of the app has the same in its Keys tab.

  python tools/keygen.py init                       create the signing key (once) and print the public key
  python tools/keygen.py issue 8F3A-21C9-77DE-0B41  print a lifetime key for that Machine ID
  python tools/keygen.py issue <id> --days 365 --note "Customer name"
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import keygen  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Grabber license key generator (seller only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("issue")
    p.add_argument("machine", help="Machine ID shown by the buyer's app")
    p.add_argument("--days", type=int, default=0, help="days valid (default 0 = lifetime)")
    p.add_argument("--note", default="", help="customer name / order, saved in issued.csv")
    args = ap.parse_args()
    try:
        if args.cmd == "init":
            print("Signing key:", keygen.PRIVATE, "(back it up somewhere safe)")
            print("Public key for license.py PUBLIC_KEY:", keygen.init())
        else:
            print(keygen.issue(args.machine, args.days, args.note))
    except keygen.KeygenError as ex:
        sys.exit(str(ex))


if __name__ == "__main__":
    main()
