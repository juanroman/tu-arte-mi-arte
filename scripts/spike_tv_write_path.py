"""Spike script (dev-only, NOT a pytest test, NOT part of the engine):
probes the write path (`upload()` + `select_image()`) of `samsungtvws`
against the real TVs, per dev_plan.md §3.1.

This is a throwaway diagnostic, not the shape of the eventual Etapa 3
integration — token persistence (3.2), multi-TV orchestration (3.3),
Frame 50 legacy handling (3.4), reversibility/watchdog (3.5), and partial
failure handling (3.6) are all explicitly out of scope here. The only
question this script answers is: can we upload an image and make it appear
on a given TV over the LAN at all, and what does pairing look like in
practice. It targets one TV per invocation — run it once per screen.

Usage:
    uv run python scripts/spike_tv_write_path.py <tv_name> [path/to/image.jpg]

    tv_name: one of 43l, 43r, 50 — resolved to a live IP via
    engine.tv_discovery.resolve_tv_host() (config/tvs.toml + mDNS fallback),
    so this no longer hardcodes IPs or trusts DHCP reservations to hold.

If no image path is given, uses the most recently generated image under
data/images/ that hasn't already been used by a prior run of this script in
the same invocation batch (see LAST_USED_FILE) — so running it back-to-back
for different TVs picks a different image each time by default. On first
run against a given TV, it may show an "Allow this device?" prompt that must
be approved with the physical remote before the script proceeds past open().

The pairing token is saved to data/tv_<name>_token.spike (gitignored) so
re-runs don't re-trigger the pairing prompt. This is a spike convenience,
not the real per-TV persistence design of §3.2.
"""

import sys
from pathlib import Path

from samsungtvws.art import SamsungTVArt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine.tv_discovery import resolve_tv_host  # noqa: E402

TV_CONFIG_NAMES = {"43l": "43L", "43r": "43R", "50": "50"}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMAGES_DIR = DATA_DIR / "images"
LAST_USED_FILE = DATA_DIR / "tv_spike_last_used.spike"


def pick_test_image() -> Path:
    candidates = sorted(IMAGES_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"No .jpg images found in {IMAGES_DIR}")

    already_used = set()
    if LAST_USED_FILE.exists():
        already_used = set(LAST_USED_FILE.read_text().splitlines())

    for candidate in reversed(candidates):
        if candidate.name not in already_used:
            return candidate

    # All recent images already used this batch — fall back to most recent.
    return candidates[-1]


def record_used(image_path: Path) -> None:
    with LAST_USED_FILE.open("a") as f:
        f.write(image_path.name + "\n")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in TV_CONFIG_NAMES:
        raise SystemExit(
            f"Usage: uv run python {sys.argv[0]} <{'|'.join(TV_CONFIG_NAMES)}> "
            "[path/to/image.jpg]"
        )
    tv_name = sys.argv[1]
    host = resolve_tv_host(TV_CONFIG_NAMES[tv_name])
    token_file = DATA_DIR / f"tv_{tv_name}_token.spike"

    image_path = Path(sys.argv[2]) if len(sys.argv) > 2 else pick_test_image()
    if not image_path.exists():
        raise SystemExit(f"Image not found: {image_path}")

    print(f"Target TV: {tv_name} @ {host}")
    print(f"Test image: {image_path}")
    print(f"Token file: {token_file} (exists: {token_file.exists()})")

    tv = SamsungTVArt(host=host, token_file=str(token_file))

    print(
        "\n[1/4] Opening websocket connection (approve 'Allow this device?' "
        "on the TV remote if prompted)..."
    )
    tv.open()
    print("Connection open.")

    print("\n[2/4] Checking Art Mode support/state...")
    print(f"  supported(): {tv.supported()}")
    print(f"  get_artmode(): {tv.get_artmode()}")

    print(f"\n[3/4] Uploading {image_path.name}...")
    content_id = tv.upload(str(image_path), matte="none", portrait_matte="none")
    print(f"  Uploaded. content_id = {content_id}")

    print(f"\n[4/4] Selecting {content_id} for display...")
    tv.select_image(content_id, show=True)
    print("  select_image() call completed.")

    record_used(image_path)
    print("\nDone. Check the TV screen to confirm the image is now showing.")
    tv.close()


if __name__ == "__main__":
    main()
