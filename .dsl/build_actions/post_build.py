"""DevStreamLabs build action: prove the player carries MediaPipe's fetched assets.

Runs after the Unity player build, before packaging. Reads the manifest
pre_build.py wrote and fails the build unless the built player
(``DSL_OUTPUT_DIR``) contains, byte for byte:

* every model/resource file in its StreamingAssets folder
  (linux/win32: ``<name>_Data/StreamingAssets``,
  macOS: ``<name>.app/Contents/Resources/Data/StreamingAssets``), and
* the native library for the target platform
  (linux: ``libmediapipe_c.so`` under ``<name>_Data/Plugins``,
  win32: ``mediapipe_c.dll`` under ``<name>_Data/Plugins``,
  macOS: ``libmediapipe_c.dylib`` under ``<name>.app/Contents/PlugIns``).

It also writes ``dsl_mediapipe_assets.json`` into the output folder, so the
shipped archive records what was verified.
"""
import hashlib
import json
import os
import pathlib
import sys

MANIFEST = "dsl_mediapipe_manifest.json"
REPORT = "dsl_mediapipe_assets.json"
NATIVE_LIB = {"linux": "libmediapipe_c.so", "win64": "mediapipe_c.dll", "win32": "mediapipe_c.dll",
              "macos": "libmediapipe_c.dylib"}


def log(msg: str) -> None:
    print(f"[mediapipe-assets] {msg}", flush=True)


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    project = pathlib.Path(os.environ.get("DSL_PROJECT_DIR") or pathlib.Path.cwd())
    output = os.environ.get("DSL_OUTPUT_DIR")
    platform = os.environ.get("DSL_PLATFORM", "")
    if not output or not pathlib.Path(output).is_dir():
        sys.exit(f"DSL_OUTPUT_DIR is not a directory: {output!r}")
    output = pathlib.Path(output)
    manifest_path = project / MANIFEST
    if not manifest_path.is_file():
        sys.exit(f"{manifest_path} missing: pre_build did not run or did not finish")
    manifest = json.loads(manifest_path.read_text())
    lib_name = NATIVE_LIB.get(platform)
    if lib_name is None:
        sys.exit(f"no native-library expectation for platform {platform!r} "
                 f"(this check covers {sorted(NATIVE_LIB)})")
    log(f"checking player in {output} for platform {platform}")

    problems = []
    streaming_dirs = [cur for cur in output.rglob("StreamingAssets") if cur.is_dir()]
    if len(streaming_dirs) != 1:
        problems.append(f"expected one StreamingAssets folder in the player, found "
                        f"{[str(cur.relative_to(output)) for cur in streaming_dirs]}")
    verified_models = 0
    if streaming_dirs:
        streaming = streaming_dirs[0]
        log(f"StreamingAssets: {streaming.relative_to(output)}")
        for name, expected in sorted(manifest["streaming_assets"].items()):
            path = streaming / name
            if not path.is_file():
                problems.append(f"missing from StreamingAssets: {name}")
            elif sha256_of(path) != expected:
                problems.append(f"content differs from the release: StreamingAssets/{name}")
            else:
                verified_models += 1

    libs = [cur for cur in output.rglob(lib_name) if cur.is_file()]
    if not libs:
        problems.append(f"native library {lib_name} is not in the player")
    else:
        for lib in libs:
            if sha256_of(lib) != manifest["native_libs"][lib_name]:
                problems.append(f"{lib.relative_to(output)} differs from the release's {lib_name}")
            else:
                log(f"native library OK: {lib.relative_to(output)} ({lib.stat().st_size} bytes)")

    if problems:
        for cur in problems:
            log(f"FAIL: {cur}")
        sys.exit(f"player is missing MediaPipe assets ({len(problems)} problem(s)); see above")

    log(f"verified {verified_models}/{len(manifest['streaming_assets'])} StreamingAssets files "
        f"and {lib_name} against release {manifest['release']} (sha256 {manifest['sha256'][:12]}...)")
    (output / REPORT).write_text(json.dumps({
        "release": manifest["release"], "release_sha256": manifest["sha256"],
        "platform": platform, "commit": os.environ.get("DSL_COMMIT"),
        "streaming_assets_dir": str(streaming_dirs[0].relative_to(output)),
        "native_libs": [str(cur.relative_to(output)) for cur in libs],
        "verified_streaming_assets": verified_models,
    }, indent=1, sort_keys=True))
    log(f"wrote {REPORT} into the player folder")


if __name__ == "__main__":
    main()
