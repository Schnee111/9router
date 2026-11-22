#!/usr/bin/env python3
"""
Install the "Antigravity ACP" provider + its Accounts card into a 9router build tree.

Idempotent. Run AFTER `git pull` and BEFORE `npm run build` — the provider list
and the dashboard are compiled into the client chunks.

  python3 apply.py /root/9r-update/build

Steps
  1. open-sse/providers/registry/antigravity-acp.js      (provider entry)
  2. register it in open-sse/providers/registry/index.js
  3. public/providers/antigravity-acp.png                (icon)
  4. src/app/api/agy-acp/[...path]/route.js              (server-side bridge proxy)
  5. providers/[id]/AcpAccountsCard.js                   (Accounts card)
  6. wire the card into the provider detail page
  7. verify registry + wiring
"""
import os
import re
import shutil
import subprocess
import sys

REG_ID = "antigravity-acp"
VAR = "pAcp"
HERE = os.path.dirname(os.path.abspath(__file__))


def copy(src_name, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(os.path.join(HERE, src_name), dst)
    print(f"  wrote {dst}")


def main(root):
    reg_dir = os.path.join(root, "open-sse", "providers", "registry")
    index = os.path.join(reg_dir, "index.js")
    if not os.path.isfile(index):
        sys.exit(f"not a 9router build tree: {index} missing")

    print("[1/7] registry entry")
    copy("antigravity-acp.js", os.path.join(reg_dir, REG_ID + ".js"))

    print("[2/7] register in index.js")
    text = open(index).read()
    imp = f'import {VAR} from "./{REG_ID}.js";'
    if imp not in text:
        lines = text.splitlines()
        last = max(i for i, ln in enumerate(lines) if ln.startswith("import "))
        lines.insert(last + 1, imp)
        text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if not re.search(r"^\s*" + VAR + r"\s*,?\s*$", text, re.M):
        text = re.sub(r"(\n\];\s*$)", f"\n  {VAR},\n];\n", text)
        if VAR not in text:
            sys.exit("could not find the registry array terminator (];)")
    open(index, "w").write(text)

    print("[3/7] icon")
    pub = os.path.join(root, "public", "providers")
    src_icon = os.path.join(pub, "antigravity.png")
    dst_icon = os.path.join(pub, REG_ID + ".png")
    if os.path.isfile(src_icon) and not os.path.isfile(dst_icon):
        shutil.copyfile(src_icon, dst_icon)

    print("[4/7] bridge proxy API route")
    copy("agy-acp-route.js",
         os.path.join(root, "src", "app", "api", "agy-acp", "[...path]", "route.js"))

    print("[5/7] Accounts card component")
    detail_dir = os.path.join(root, "src", "app", "(dashboard)", "dashboard",
                              "providers", "[id]")
    copy("AcpAccountsCard.js", os.path.join(detail_dir, "AcpAccountsCard.js"))

    print("[6/7] wire card into the detail page")
    page = os.path.join(detail_dir, "page.js")
    src = open(page).read()
    if 'from "./AcpAccountsCard"' not in src:
        anchor = 'import CustomConfigCard from "./CustomConfigCard";'
        if anchor not in src:
            sys.exit("detail page import anchor missing — upstream changed, re-check")
        src = src.replace(anchor, anchor + '\nimport AcpAccountsCard from "./AcpAccountsCard";', 1)
    if "<AcpAccountsCard />" not in src:
        marker = "      {isFreeNoAuth ? (\n        <NoAuthProxyCard providerId={providerId} />\n      ) : ("
        if marker not in src:
            sys.exit("detail page render anchor missing — upstream changed, re-check")
        replacement = (
            "      {isFreeNoAuth ? (\n"
            "        <>\n"
            f"          {{providerId === \"{REG_ID}\" && <AcpAccountsCard />}}\n"
            "          <NoAuthProxyCard providerId={providerId} />\n"
            "        </>\n"
            "      ) : ("
        )
        src = src.replace(marker, replacement, 1)
    open(page, "w").write(src)

    print("[7/7] verify")
    check = f"""
import REG from '{index}';
const hit = REG.find(r => r.id === '{REG_ID}');
if (!hit) {{ console.error('MISSING registry entry'); process.exit(1); }}
console.log('registry OK:', hit.id, '|', hit.display.name, '|', hit.transport.baseUrl, '|', hit.models.length, 'models');
"""
    tmp = os.path.join(root, ".acp-verify.mjs")
    open(tmp, "w").write(check)
    try:
        r = subprocess.run(["node", tmp], capture_output=True, text=True, cwd=root, timeout=60)
        print("  " + (r.stdout or r.stderr).strip()[:400])
        if r.returncode != 0:
            sys.exit("registry verification failed")
    finally:
        os.unlink(tmp)
    wired = open(page).read()
    for needle in ("import AcpAccountsCard", "<AcpAccountsCard />"):
        if needle not in wired:
            sys.exit(f"detail page wiring missing: {needle}")
    print("  detail page wiring OK")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/root/9r-update/build")
