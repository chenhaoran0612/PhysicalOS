#!/usr/bin/env bash
# Run check.py inside the ROS 2 Humble image against real rclpy nodes (issue #21). See README.md.
#
#   runtime/tests/ros2_humble/run.sh [output directory]      (default: ./artifacts/ros2_humble)
#
# Needs Docker and the image ros:humble-ros-base-jammy. No network: the container gets none.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="$(mkdir -p "${1:-$REPO/artifacts/ros2_humble}" && cd "${1:-$REPO/artifacts/ros2_humble}" && pwd)"
IMAGE="${ROS2_HUMBLE_IMAGE:-ros:humble-ros-base-jammy}"

docker run --rm --network none --entrypoint bash \
  -v "$REPO":/src:ro -v "$OUT":/out "$IMAGE" -c '
    set -e
    mkdir /work
    cd /src && tar cf - --exclude=.git --exclude=.build --exclude=dist --exclude=node_modules --exclude=artifacts \
      runtime spec | tar xf - -C /work
    set +u
    . /opt/ros/humble/setup.bash
    export PYTHONDONTWRITEBYTECODE=1
    exec python3 /work/runtime/tests/ros2_humble/check.py /work /out'
