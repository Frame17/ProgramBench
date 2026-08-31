#!/bin/bash

set -euo pipefail

cargo build --release
mv target/release/mdbook executable
