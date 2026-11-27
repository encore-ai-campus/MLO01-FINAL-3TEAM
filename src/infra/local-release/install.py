"""Install the outbound release watcher for the logged-in macOS user."""
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
from watch import STATE

if __name__ == "__main__":
    live = Path(sys.argv[1]).expanduser().resolve()
    if not (live / "web.json").is_file():
        raise SystemExit("Usage: python3 install.py /absolute/path/to/live-ticket-check")
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    initial = STATE / "initial"
    if not initial.exists():
        sha = subprocess.check_output(["docker", "inspect", "-f",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}', "voc-live-ticket-web"], text=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SystemExit("Running web image needs a Git revision label")
        initial.write_text(sha + "\n")
    watcher = STATE / "watch.py"
    shutil.copyfile(Path(__file__).with_name("watch.py"), watcher.with_suffix(".tmp"))
    os.replace(watcher.with_suffix(".tmp"), watcher)
    label = "local.voc.develop-release"
    target = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(plistlib.dumps(dict(Label=label, RunAtLoad=True, StartInterval=300,
        ProgramArguments=[sys.executable, str(watcher)], WorkingDirectory=str(STATE),
        StandardOutPath=str(STATE / "release.log"), StandardErrorPath=str(STATE / "release.log"),
        EnvironmentVariables=dict(PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                  VOC_RELEASE_HOME=str(STATE), VOC_LIVE=str(live)))))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"{label} — {STATE / 'release.log'}")
