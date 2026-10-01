"""DevStreamLabs build action: fetch MediaPipe's binary assets before the Unity build.

The repository commits the .meta files of the native libraries, the Protobuf
DLLs and the ~40 model files, but not the files themselves (they are
gitignored upstream). They ship in the plugin's release tarball, so this
script:

1. downloads the pinned release tarball (or reuses a cached copy),
2. verifies its sha256 against the pin below -- a mismatch fails the build,
3. extracts ONLY ``package/Runtime/Plugins/**`` and
   ``package/PackageResources/MediaPipe/**`` into
   ``Packages/com.github.homuler.mediapipe/``, never overwriting a committed
   ``.meta`` file (the GUIDs the scenes reference live there),
4. copies the model files into ``Assets/StreamingAssets/`` -- the sample app's
   AppSettings uses the StreamingAssets loader, so that is where a player
   reads them at run time,
5. writes ``dsl_mediapipe_manifest.json`` (project root, outside Assets/) with
   the sha256 of every file it placed, which post_build.py checks the player
   against.

Standard library only; runs on linux, darwin and win32.
"""
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

VERSION = "0.16.3"
URL = ("https://github.com/homuler/MediaPipeUnityPlugin/releases/download/"
       f"v{VERSION}/com.github.homuler.mediapipe-{VERSION}.tgz")
SHA256 = "cc3e77a219e0b99618ae3be64c31a566197deedc69c1e136acf52d65d7cf2e79"
SIZE = 290_414_365

PACKAGE = "Packages/com.github.homuler.mediapipe"
WANTED_PREFIXES = ("package/Runtime/Plugins/", "package/PackageResources/MediaPipe/")
MODELS_PREFIX = "package/PackageResources/MediaPipe/"
STREAMING_ASSETS = "Assets/StreamingAssets"
MANIFEST = "dsl_mediapipe_manifest.json"
# Files the build cannot do without; checked after extraction.
REQUIRED = ("Runtime/Plugins/Protobuf/Google.Protobuf.dll",
            "Runtime/Plugins/libmediapipe_c.so",
            "Runtime/Plugins/mediapipe_c.dll",
            "Runtime/Plugins/libmediapipe_c.dylib")


def log(msg: str) -> None:
    print(f"[mediapipe-assets] {msg}", flush=True)


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_dir() -> pathlib.Path:
    """A per-user cache outside the build workspace, so repeat builds on one
    machine skip the 277 MB download. Build actions offer no cache directory
    of their own; every use is re-verified against the pinned sha256, so a
    corrupt or tampered cache entry is discarded and re-fetched."""
    override = os.environ.get("MEDIAPIPE_ASSET_CACHE")
    if override:
        return pathlib.Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(pathlib.Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "dsl-build-actions" / "mediapipe"


def download(dest: pathlib.Path) -> None:
    tmp = dest.with_name(dest.name + f".part-{os.getpid()}")
    log(f"downloading {URL}")
    started = time.monotonic()
    last_report = started
    done = 0
    req = urllib.request.Request(URL, headers={"User-Agent": "dsl-build-action"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if time.monotonic() - last_report > 10:
                last_report = time.monotonic()
                log(f"  {done / 1e6:.0f} / {SIZE / 1e6:.0f} MB")
    log(f"downloaded {done} bytes in {time.monotonic() - started:.1f} s")
    os.replace(tmp, dest)


def fetch_verified() -> pathlib.Path:
    cache = cache_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        probe = cache / f".write-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        log(f"cache {cache} not writable ({error}); downloading into the work directory")
        cache = pathlib.Path(tempfile.mkdtemp(prefix="mediapipe-", dir=os.environ.get("DSL_WORK_DIR")))
    tarball = cache / f"com.github.homuler.mediapipe-{VERSION}-{SHA256[:16]}.tgz"
    if tarball.is_file():
        actual = sha256_of(tarball)
        if actual == SHA256:
            log(f"cache hit: {tarball}")
            log(f"sha256 OK {actual}")
            return tarball
        log(f"cache entry {tarball} has sha256 {actual}, expected {SHA256}; discarding it")
        tarball.unlink()
    else:
        log(f"cache miss: {tarball}")
    download(tarball)
    actual = sha256_of(tarball)
    if actual != SHA256:
        tarball.unlink()
        sys.exit(f"sha256 MISMATCH for {URL}: got {actual}, pinned {SHA256}")
    log(f"sha256 OK {actual}")
    return tarball


def extract(tarball: pathlib.Path, package_dir: pathlib.Path) -> dict:
    """Extract the wanted members; returns {relative path: sha256} of the files placed."""
    placed: dict = {}
    kept_meta = 0
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar:
            name = member.name
            if not name.startswith(WANTED_PREFIXES) or not member.isfile():
                continue
            rel = pathlib.PurePosixPath(name[len("package/"):])
            if ".." in rel.parts or rel.is_absolute():
                sys.exit(f"refusing unsafe archive member {name!r}")
            target = package_dir.joinpath(*rel.parts)
            if name.endswith(".meta") and target.exists():
                kept_meta += 1  # committed .meta wins: it carries the GUIDs the project references
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            digest = hashlib.sha256()
            tmp = target.with_name(target.name + ".part")
            with open(tmp, "wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    digest.update(chunk)
                    out.write(chunk)
            os.replace(tmp, target)
            if not name.endswith(".meta"):
                placed[rel.as_posix()] = digest.hexdigest()
    log(f"extracted {len(placed)} files into {package_dir} "
        f"(kept {kept_meta} committed .meta files untouched)")
    return placed


def main() -> None:
    project = pathlib.Path(os.environ.get("DSL_PROJECT_DIR") or pathlib.Path.cwd())
    package_dir = project / PACKAGE
    if not (package_dir / "package.json").is_file():
        sys.exit(f"{package_dir} is not the MediaPipe package (no package.json)")
    log(f"platform={os.environ.get('DSL_PLATFORM')} host={os.environ.get('DSL_HOST_OS')} "
        f"unity={os.environ.get('DSL_TOOLCHAIN_VERSION')} project={project}")

    tarball = fetch_verified()
    placed = extract(tarball, package_dir)

    missing = [cur for cur in REQUIRED if cur not in placed]
    if missing:
        sys.exit(f"release {VERSION} lacks required files: {missing}")

    models = {rel: digest for rel, digest in placed.items()
              if rel.startswith(MODELS_PREFIX[len("package/"):])}
    streaming = project / STREAMING_ASSETS
    streaming.mkdir(parents=True, exist_ok=True)
    for rel in models:
        shutil.copyfile(package_dir / rel, streaming / pathlib.PurePosixPath(rel).name)
    log(f"copied {len(models)} model/resource files into {streaming}")

    manifest = {
        "release": VERSION, "url": URL, "sha256": SHA256,
        "streaming_assets": {pathlib.PurePosixPath(rel).name: digest for rel, digest in models.items()},
        "native_libs": {pathlib.PurePosixPath(rel).name: digest for rel, digest in placed.items()
                        if rel.startswith("Runtime/Plugins/") and "/Protobuf/" not in rel
                        and pathlib.PurePosixPath(rel).name in ("libmediapipe_c.so", "mediapipe_c.dll",
                                                                 "libmediapipe_c.dylib")},
    }
    (project / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True))
    log(f"wrote {project / MANIFEST}: {len(manifest['streaming_assets'])} streaming assets, "
        f"native libs {sorted(manifest['native_libs'])}")
    log("done")


if __name__ == "__main__":
    main()
