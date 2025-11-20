#!/usr/bin/env python3
"""Setup runner for Houdini integration tests in CodeBuild."""
import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

HOUDINI_VERSIONS = ["19.5.805", "20.0.896", "20.5.613", "21.0.440"]

def run(cmd, shell=False, check=True):
    print(f"Running: {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    result = subprocess.run(cmd, shell=shell)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result


def download_from_s3(s3_path, local_path):
    bucket = os.environ.get("DEPENDENCY_BUCKET")
    if not bucket:
        print("ERROR: DEPENDENCY_BUCKET not set")
        sys.exit(1)
    run(["aws", "s3", "cp", f"s3://{bucket}/{s3_path}", str(local_path), "--no-progress"])


def setup_linux():
    for version in HOUDINI_VERSIONS:
        major_minor = ".".join(version.split(".")[:2])
        houdini_dir = Path(f"/opt/hfs{version}")
        houdini_marker = houdini_dir / ".installed"

        if houdini_marker.exists():
            print(f"Houdini {version} already installed")
            continue

        lock_file = Path(f"/tmp/houdini-{version}.lock")
        if lock_file.exists():
            print(f"Waiting for concurrent Houdini {version} install...")
            for _ in range(120):
                time.sleep(1)
                if houdini_marker.exists():
                    break
            continue

        lock_file.touch()
        try:
            print(f"Installing Houdini {version}...")
            houdini_archive = Path(f"/tmp/houdini-{version}.tar.gz")
            download_from_s3(f"houdini/houdini-{version}-linux_x86_64_gcc11.2.tar.gz", houdini_archive)

            run(f"tar -xf {houdini_archive} -C /tmp", shell=True)
            install_dir = Path(f"/tmp/houdini-{version}-linux_x86_64_gcc11.2")
            run(f"chmod -R 777 {install_dir}", shell=True)
            
            houdini_dir.parent.mkdir(parents=True, exist_ok=True)
            run(f"chmod 777 {houdini_dir.parent}", shell=True)
            
            run(
                f'cd {install_dir} && echo "y" | ./houdini.install --auto-install --install-houdini --no-install-license --no-install-menus --no-install-bin-symlink --install-hfs-symlink --no-install-hqueue-server --no-root-check --accept-EULA 2021-10-13 {houdini_dir}',
                shell=True,
            )
            
            houdini_marker.touch()
            houdini_archive.unlink(missing_ok=True)
            run(f"rm -rf {install_dir}", shell=True, check=False)
        finally:
            lock_file.unlink(missing_ok=True)

    print("Installing Houdini submitter...")
    run("hatch build", shell=True)
    for version in HOUDINI_VERSIONS:
        major_minor = ".".join(version.split(".")[:2])
        run(f"hatch run install --houdini-version {major_minor}", shell=True)


def setup_windows():
    for version in HOUDINI_VERSIONS:
        houdini_dir = Path(f"C:/Tools/houdini-{version}")
        houdini_marker = houdini_dir / ".installed"

        if houdini_marker.exists():
            print(f"Houdini {version} already installed")
            continue

        lock_file = Path(f"C:/Temp/houdini-{version}.lock")
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        if lock_file.exists():
            print(f"Waiting for concurrent Houdini {version} install...")
            for _ in range(120):
                time.sleep(1)
                if blender_marker.exists():
                    break
            continue

        lock_file.touch()
        try:
            print(f"Installing Houdini {version}...")
            houdini_installer = Path(f"C:/Tools/houdini-{version}-installer.exe")
            houdini_installer.parent.mkdir(parents=True, exist_ok=True)
            
            download_from_s3(f"houdini/houdini-{version}-win64-vc143.exe", houdini_installer)
            
            run(
                f'"{houdini_installer}" /S /AcceptEULA=Yes /InstallDir={houdini_dir} /InstallHoudini=Yes /InstallLicense=No /InstallMenus=No',
                shell=True,
            )
            run("timeout /t 120 /nobreak", shell=True)
            
            houdini_marker.touch()
            houdini_installer.unlink(missing_ok=True)
        finally:
            lock_file.unlink(missing_ok=True)

    print("Installing Houdini submitter...")
    run("hatch build", shell=True)
    for version in HOUDINI_VERSIONS:
        major_minor = ".".join(version.split(".")[:2])
        run(f"hatch run install --houdini-version {major_minor}", shell=True)


def setup_macos():
    for version in HOUDINI_VERSIONS:
        houdini_dir = Path(f"/Applications/Houdini/Houdini{version}")
        houdini_marker = houdini_dir / ".installed"

        if houdini_marker.exists():
            print(f"Houdini {version} already installed")
            continue

        print(f"Installing Houdini {version}...")
        houdini_dmg = Path(f"/tmp/houdini-{version}.dmg")
        
        download_from_s3(f"houdini/houdini-{version}-macosx_arm64_clang.dmg", houdini_dmg)
        
        run(f"hdiutil attach {houdini_dmg}", shell=True)
        run(f"sudo rm -rf {houdini_dir}", shell=True, check=False)
        run(f"sudo cp -R /Volumes/Houdini*/Houdini* {houdini_dir.parent}", shell=True)
        run("hdiutil detach /Volumes/Houdini*", shell=True, check=False)
        
        houdini_marker.touch()
        houdini_dmg.unlink(missing_ok=True)

    print("Installing Houdini submitter...")
    run("hatch build", shell=True)
    for version in HOUDINI_VERSIONS:
        major_minor = ".".join(version.split(".")[:2])
        run(f"hatch run install --houdini-version {major_minor}", shell=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup Houdini test environment")
    parser.add_argument("--versions", nargs="+", help="Houdini versions to install")
    args = parser.parse_args()

    if args.versions:
        HOUDINI_VERSIONS = args.versions

    system = platform.system()
    print(f"Setting up {system} with Houdini {', '.join(HOUDINI_VERSIONS)}")
    
    if system == "Linux":
        setup_linux()
    elif system == "Windows":
        setup_windows()
    elif system == "Darwin":
        setup_macos()
    else:
        print(f"Unsupported platform: {system}")
        sys.exit(1)
    
    print("Setup complete!")
