#!/usr/bin/env python3
"""Publishes new Vision OR headset builds from Xcode Cloud to this site.

Runs in this repo's GitHub Action. For each finished run of the "Vision OR
Headset" Xcode Cloud workflow that isn't published yet, it downloads the
development-signed visionOS export, checks it, attaches it to a GitHub release,
writes its install manifest, and rebuilds the page. Every build stays listed.

Needs ASC_KEY_ID, ASC_ISSUER_ID and ASC_PRIVATE_KEY (the .p8 file's contents),
and GH_TOKEN for the gh CLI. The source lives in the private Vision OR repo
under apps/prototypes/visionor-apple/Tools/headset-builds/site.
"""
import base64
import html
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

WORKFLOW_ID = "db4f26e0-9f5b-4632-970a-b23c301219c7"  # "Vision OR Headset"
REPO = os.environ.get("GITHUB_REPOSITORY", "3m/visionor-headset")
SITE = f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}"
ROOT = Path(__file__).resolve().parent
BUILDS = ROOT / "builds.json"
API = "https://api.appstoreconnect.apple.com/v1"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def asc_token() -> str:
    """A 20-minute App Store Connect token, signed with openssl."""
    now = int(time.time())
    header = b64(json.dumps({"alg": "ES256", "kid": os.environ["ASC_KEY_ID"], "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 1200,
                             "aud": "appstoreconnect-v1"}).encode())
    with tempfile.NamedTemporaryFile("w", suffix=".p8", delete=False) as key:
        key.write(os.environ["ASC_PRIVATE_KEY"])
    try:
        der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key.name],
                             input=f"{header}.{claims}".encode(), capture_output=True, check=True).stdout
    finally:
        os.unlink(key.name)

    # openssl signs in DER; a JWT wants the raw r and s, 32 bytes each.
    def read_int(buf, i):
        n = buf[i + 1]
        return int.from_bytes(buf[i + 2:i + 2 + n], "big"), i + 2 + n
    r, i = read_int(der, 2 if der[1] < 0x80 else 3)
    s, _ = read_int(der, i)
    return f"{header}.{claims}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def asc(path: str, token: str) -> dict:
    request = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def development_export(run_id: str, token: str) -> str | None:
    """The download URL of the run's development-signed visionOS export."""
    for action in asc(f"/ciBuildRuns/{run_id}/actions", token)["data"]:
        if action["attributes"]["actionType"] != "ARCHIVE":
            continue
        for artifact in asc(f"/ciBuildActions/{action['id']}/artifacts", token)["data"]:
            attributes = artifact["attributes"]
            if attributes["fileType"] == "ARCHIVE_EXPORT" and "development" in attributes["fileName"]:
                return attributes["downloadUrl"]
    return None


def extract_ipa(download_url: str, folder: Path) -> Path:
    """Downloads the export and returns the .ipa inside it."""
    archive = folder / "export.zip"
    urllib.request.urlretrieve(download_url, archive)
    with zipfile.ZipFile(archive) as export:
        name = next(n for n in export.namelist() if n.endswith(".ipa"))
        export.extract(name, folder)
    return folder / name


def check(ipa: Path) -> tuple[str, str]:
    """Refuses a build with API keys or without the enterprise entitlements.
    Returns its bundle ID and version."""
    with zipfile.ZipFile(ipa) as app:
        names = app.namelist()
        if any(n.endswith(".app/VoiceKeys.plist") for n in names):
            sys.exit(f"{ipa.name} contains VoiceKeys.plist. Headset builds are public, so they must not carry API keys.")
        profile = next(n for n in names if n.count("/") == 2 and n.endswith(".app/embedded.mobileprovision"))
        if b"com.apple.developer.arkit.main-camera-access.allow" not in app.read(profile):
            sys.exit(f"{ipa.name} isn't signed with the enterprise entitlements, so it has no Room Camera.")
        info = plistlib.loads(app.read(next(n for n in names if n.count("/") == 2 and n.endswith(".app/Info.plist"))))
    return info["CFBundleIdentifier"], info["CFBundleShortVersionString"]


def manifest(ipa_url: str, bundle_id: str, version: str, title: str) -> bytes:
    """The install manifest. It leaves out platform-identifier, because
    visionOS refuses one that names xros."""
    return plistlib.dumps({"items": [{
        "assets": [{"kind": "software-package", "url": ipa_url}],
        "metadata": {"bundle-identifier": bundle_id, "bundle-version": version, "kind": "software", "title": title},
    }]})


def install_link(number: int) -> str:
    return f"itms-services://?action=download-manifest&url={SITE}/manifests/{number}.plist"


def render(builds: list[dict]) -> str:
    rows = []
    for index, build in enumerate(builds):
        date = datetime.fromisoformat(build["date"]).strftime("%b %-d, %Y %-I:%M %p UTC")
        badge = '<span class="badge">Latest</span>' if index == 0 else ""
        rows.append(f"""
    <li class="build">
      <div class="info">
        <div class="title">Build {build['number']} {badge}</div>
        <div class="meta">Vision OR {html.escape(build['version'])} · {date}</div>
        <div class="change">{html.escape(build['message'])}</div>
      </div>
      <a class="install" href="{html.escape(install_link(build['number']))}">Install</a>
    </li>""")
    template = (ROOT / "page.html").read_text()
    return template.replace("{{builds}}", "".join(rows) or '<li class="empty">No builds yet.</li>')


def main() -> None:
    builds = json.loads(BUILDS.read_text()) if BUILDS.exists() else []
    published = {b["number"] for b in builds}
    token = asc_token()
    runs = asc(f"/ciWorkflows/{WORKFLOW_ID}/buildRuns?limit=20", token)["data"]
    new = [r for r in runs if r["attributes"]["completionStatus"] == "SUCCEEDED"
           and r["attributes"]["number"] not in published]
    for run in sorted(new, key=lambda r: r["attributes"]["number"]):
        attributes = run["attributes"]
        number = attributes["number"]
        url = development_export(run["id"], token)
        if url is None:
            print(f"Build {number} has no development export. Skipped.")
            continue
        with tempfile.TemporaryDirectory() as folder:
            ipa = extract_ipa(url, Path(folder))
            bundle_id, version = check(ipa)
            release = f"build-{number}"
            asset = Path(folder) / "VisionOR_App.ipa"
            ipa.rename(asset)
            commit = attributes.get("sourceCommit") or {}
            message = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
            subprocess.run(["gh", "release", "create", release, str(asset), "--repo", REPO,
                            "--title", f"Build {number}", "--notes", message], check=True)
        ipa_url = f"https://github.com/{REPO}/releases/download/{release}/VisionOR_App.ipa"
        (ROOT / "manifests").mkdir(exist_ok=True)
        (ROOT / "manifests" / f"{number}.plist").write_bytes(
            manifest(ipa_url, bundle_id, version, f"Vision OR {version} ({number})"))
        builds.append({"number": number, "version": version, "message": message,
                       "date": attributes.get("finishedDate") or attributes["createdDate"],
                       "commit": (commit.get("commitSha") or "")[:7]})
        # A large download can outlast the 20-minute token.
        token = asc_token()
        print(f"Published build {number}")

    builds.sort(key=lambda b: b["number"], reverse=True)
    BUILDS.write_text(json.dumps(builds, indent=2) + "\n")
    if builds:
        (ROOT / "manifests" / "latest.plist").write_bytes(
            (ROOT / "manifests" / f"{builds[0]['number']}.plist").read_bytes())
    (ROOT / "index.html").write_text(render(builds))


if __name__ == "__main__":
    main()
