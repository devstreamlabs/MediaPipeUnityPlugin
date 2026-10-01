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


def macho_segments(path: pathlib.Path) -> dict:
    """{(cpu type, segment, section): sha256 of the section's bytes} for every
    64-bit Mach-O slice; __LINKEDIT (signature, symbol tables) excluded.

    A macOS player build re-signs every plugin it copies into Contents/PlugIns
    (ad hoc), which rewrites the code signature in __LINKEDIT and the load
    command pointing at it (in the header at the start of __TEXT), so the
    shipped dylib is never byte-identical to the release's. Every section --
    the code and data -- must be."""
    import struct
    data = path.read_bytes()
    slices = []
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in (0xCAFEBABE, 0xCAFEBABF):  # fat (universal), big-endian header
        count = struct.unpack_from(">I", data, 4)[0]
        wide = magic == 0xCAFEBABF
        for i in range(count):
            if wide:
                _cpu, _sub, off, size = struct.unpack_from(">iiQQ", data, 8 + i * 32)[:4]
            else:
                _cpu, _sub, off, size = struct.unpack_from(">iiII", data, 8 + i * 20)[:4]
            slices.append(data[off:off + size])
    else:
        slices.append(data)
    out = {}
    for blob in slices:
        if struct.unpack_from("<I", blob, 0)[0] != 0xFEEDFACF:
            raise ValueError(f"{path.name}: not a 64-bit little-endian Mach-O slice")
        cpu, _sub, _ftype, ncmds = struct.unpack_from("<iiII", blob, 4)
        pos = 32
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from("<II", blob, pos)
            if cmd == 0x19:  # LC_SEGMENT_64
                segment = blob[pos + 8:pos + 24].rstrip(b"\0").decode()
                nsects = struct.unpack_from("<I", blob, pos + 64)[0]
                for index in range(nsects if segment != "__LINKEDIT" else 0):
                    base = pos + 72 + index * 80
                    section = blob[base:base + 16].rstrip(b"\0").decode()
                    size, offset = struct.unpack_from("<QI", blob, base + 40)
                    if offset:  # zero-fill sections have no file bytes
                        out[(cpu, segment, section)] = hashlib.sha256(blob[offset:offset + size]).hexdigest()
            pos += cmdsize
    return out


def same_library(shipped: pathlib.Path, source: pathlib.Path) -> str:
    """'' if the shipped library is the release's; otherwise why not."""
    if sha256_of(shipped) == sha256_of(source):
        return ""
    if shipped.suffix == ".dylib":
        shipped_segments, source_segments = macho_segments(shipped), macho_segments(source)
        if shipped_segments and shipped_segments == source_segments:
            log(f"{shipped.name}: every code/data section of every architecture matches the release "
                f"({len(source_segments)} sections); only the code signature differs (Unity re-signs plugins)")
            return ""
        return (f"differs from the release's in its code or data "
                f"(sections {sorted(set(shipped_segments) ^ set(source_segments)) or "with different bytes"})")
    return "differs from the release's"


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
        source = project / "Packages/com.github.homuler.mediapipe/Runtime/Plugins" / lib_name
        if not source.is_file() or sha256_of(source) != manifest["native_libs"][lib_name]:
            sys.exit(f"{source} is not the file pre_build placed; cannot compare")
        for lib in libs:
            why = same_library(lib, source)
            if why:
                problems.append(f"{lib.relative_to(output)} {why}")
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
