"""Exercise an installed wheel outside the checkout, in a fresh environment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()
    wheels = sorted(Path(args.dist).resolve().glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one wheel in the distribution directory")
    with tempfile.TemporaryDirectory(prefix="agent-eval-wheel-") as directory:
        root = Path(directory)
        env = root / "env"
        venv.EnvBuilder(with_pip=True).create(env)
        python = env / ("Scripts/python.exe" if (env / "Scripts").exists() else "bin/python")

        def run(*arguments, success=True):
            result = subprocess.run([str(python), "-I", *arguments], cwd=root, capture_output=True, text=True)
            if (result.returncode == 0) != success:
                raise RuntimeError(f"Installed distribution check failed: {arguments}\n{result.stdout}\n{result.stderr}")
            return result

        run("-m", "pip", "install", str(wheels[0]))
        run("-m", "harness", "init", "demo")
        run("-m", "harness", "eval", "--config", "demo/agent-eval.toml", "--runs", "2")
        for pack in ("coding-starter@1.0.0", "json-contracts@1.0.0"):
            inspected = run("-m", "harness", "pack", "inspect", pack)
            assert len(json.loads(inspected.stdout)["fingerprint"]) == 64
        run("-m", "harness", "replay", "--result", "demo/results/run.json", "--html", "replay.html")
        run("-m", "harness", "rescore", "--result", "demo/results/run.json", "--scorers", "contains",
            "--output", "rescored.json", "--html", "rescored.html", "--min-pass-rate", "1")
        run("-m", "harness", "gate", "--result", "rescored.json", "--min-pass-rate", "1")
        run("-m", "harness", "gate", "--result", "rescored.json", "--max-cost-usd", "100", success=False)
        print("Installed wheel: init, repeated eval, bundled packs, replay, rescore and gates passed.")


if __name__ == "__main__":
    main()
