#!/bin/sh
set -eu
repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
task_dir=$(CDPATH='' cd -- "$repo_dir/../.." && pwd)
if command -v cargo >/dev/null 2>&1; then
    cargo_command=cargo
elif [ -x "$task_dir/work/cargo/bin/cargo" ]; then
    export RUSTUP_HOME="$task_dir/work/rustup"
    export CARGO_HOME="$task_dir/work/cargo"
    cargo_command="$CARGO_HOME/bin/cargo"
else
    echo 'Install a stable Rust toolchain, then rerun.' >&2
    exit 1
fi
exec "$cargo_command" build --manifest-path "$repo_dir/Cargo.toml" --locked --release --bin leadlag
