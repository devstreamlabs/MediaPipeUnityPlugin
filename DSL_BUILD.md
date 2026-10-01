# Building this fork on DevStreamLabs

Branch `dsl-build` builds the sample app (the scenes in `ProjectSettings/EditorBuildSettings.asset`)
as a Linux, Windows or macOS standalone player on the DevStreamLabs build platform, Unity
**6000.3.20f1**.

## Why there are build actions

Upstream keeps the binaries out of git: the native libraries (`libmediapipe_c.so`,
`mediapipe_c.dll`, `libmediapipe_c.dylib`, the Android `.aar`, the iOS framework), the Protobuf
DLLs and ~40 model files under `PackageResources/MediaPipe/` are gitignored, with only their
`.meta` files committed. Without them the C# compile fails (`Google.Protobuf` is missing). They ship
in the plugin's release tarball, so the build fetches them:

| Script | Hook | What it does |
|---|---|---|
| `.dsl/build_actions/pre_build.py` | before the Unity build | Downloads `com.github.homuler.mediapipe-0.16.3.tgz` from the upstream GitHub release (or reuses a per-user cached copy), checks its pinned sha256, extracts only `Runtime/Plugins/**` and `PackageResources/MediaPipe/**` into `Packages/com.github.homuler.mediapipe/` without overwriting committed `.meta` files, copies the models into `Assets/StreamingAssets/` (the sample's `AppSettings` uses the StreamingAssets loader), and writes `dsl_mediapipe_manifest.json` with the hash of every file it placed. |
| `.dsl/build_actions/post_build.py` | after the player build | Fails the build unless the player's StreamingAssets folder holds every model and the platform's native library is in the player, all byte-identical to the release. Writes `dsl_mediapipe_assets.json` into the shipped player. |

The scripts use only the Python standard library. The cache lives at
`$XDG_CACHE_HOME`/`~/.cache` (Linux), `~/Library/Caches` (macOS) or `%LOCALAPPDATA%` (Windows)
under `dsl-build-actions/mediapipe/`, or at `MEDIAPIPE_ASSET_CACHE` if that variable is set. Every
use is re-verified, so a corrupt entry is discarded and downloaded again.

To move to another plugin release, change `VERSION`, `SHA256` and `SIZE` in `pre_build.py`.

Build actions must be enabled for the organization and project by DevStreamLabs support. See the
platform's build actions guide.

## Other changes from upstream `master`

- `ProjectSettings/ProjectVersion.txt`: 6000.0.58f2 to 6000.3.20f1. The Linux build machines need
  6000.0.81f1 or later (see the platform notes), and 6000.3.20f1 is installed on every build OS.
