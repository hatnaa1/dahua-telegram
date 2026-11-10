"""Dahua configManager CLI. Secrets are never included in diagnostic output."""
import argparse
import os
import sys
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv


class CameraError(Exception):
    pass


def masked(config):
    return {key: ("********" if any(word in key.lower() for word in
                ("password", "passwd", "token", "secret", "credential")) else value)
            for key, value in config.items()}


class Camera:
    def __init__(self, host, user, password):
        # Host only: do not permit credentials, paths, or arbitrary schemes in env.
        import ipaddress
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise CameraError("DAHUA_IP must be an IPv4 address") from None
        self.url = f"http://{host}/cgi-bin/configManager.cgi"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = HTTPDigestAuth(user, password)

    def request(self, params):
        try:
            response = self.session.get(self.url, params=params, timeout=(5, 20),
                                        allow_redirects=False)
            if response.status_code != 200:
                raise CameraError(f"Camera HTTP status {response.status_code}")
            result = response.text.strip()
            if not result or result.lower().startswith(("error", "bad request")):
                raise CameraError("Camera rejected configManager request")
            return result
        except requests.RequestException:
            # Exception strings and request URLs may contain Email.Password.
            raise CameraError("Camera connection failed; check LAN, credentials and HTTP API") from None

    def read(self, name):
        result = self.request({"action": "getConfig", "name": name})
        values = {}
        for line in result.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                key = key.removeprefix("table.")
                if key.startswith(name + ".") or key.startswith(name + "["):
                    values[key] = value.strip()
        if not values:
            raise CameraError(f"No supported {name} configuration returned")
        return values

    def write(self, values):
        if self.request({"action": "setConfig", **values}) != "OK":
            raise CameraError("Camera did not confirm setConfig; inspect masked --show output")


def desired_config(env, motion_level=None):
    user, password = env.get("SMTP_USER", ""), env.get("SMTP_PASS", "")
    if bool(user) != bool(password):
        raise CameraError("Set both SMTP_USER and SMTP_PASS, or leave both empty")
    level = str(motion_level if motion_level is not None else env.get("DAHUA_MOTION_LEVEL", "3"))
    if level not in {str(n) for n in range(1, 7)}:
        raise CameraError("Motion level must be 1..6")
    port = env.get("DAHUA_SMTP_PORT", "25")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise CameraError("DAHUA_SMTP_PORT must be 1..65535")
    return {
        "Email.Enable": "true",
        "Email.Address": env.get("DAHUA_SMTP_SERVER", "192.168.1.245"),
        "Email.Port": port,
        "Email.UserName": user,
        "Email.Password": password,
        "Email.Anonymous": str(not bool(user)).lower(),
        "Email.SendAddress": env.get("DAHUA_SENDER", "dahua-front@camera.local"),
        "Email.Receivers[0]": env.get("DAHUA_RECEIVER", "alerts@camera.local"),
        "Email.Title": env.get("DAHUA_TITLE", "Dahua Front Door"),
        "Email.AttachEnable": "true",
        "Email.SslEnable": "false",
        "MotionDetect[0].Enable": "true",
        "MotionDetect[0].Level": level,
        "MotionDetect[0].EventHandler.MailEnable": "true",
        "MotionDetect[0].EventHandler.SnapshotEnable": "true",
    }


def apply(camera, values, current):
    missing = values.keys() - current.keys()
    if missing:
        raise CameraError("Firmware fields missing: " + ", ".join(sorted(missing)) +
                          "; use Web UI instead. No changes sent.")
    camera.write(values)
    actual = {**camera.read("Email"), **camera.read("MotionDetect")}
    # Some firmware masks password on read; never compare or print it.
    mismatch = [key for key, value in values.items()
                if "password" not in key.lower() and actual.get(key) != value]
    if mismatch:
        raise CameraError("Read-back mismatch: " + ", ".join(mismatch) +
                          ". Camera may be partially configured; inspect --show.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true", help="Read Email and MotionDetect[0], mask secrets")
    group.add_argument("--configure", "--apply", action="store_true", help="Enable SMTP snapshots and motion")
    group.add_argument("--disable", action="store_true", help="Disable motion detection and its email linkage")
    parser.add_argument("--motion-level", type=int, choices=range(1, 7))
    args = parser.parse_args(argv)
    load_dotenv(args.env_file, override=False, interpolate=False)
    try:
        password = os.getenv("DAHUA_PASSWORD") or os.getenv("DAHUA_PASS")
        user = os.getenv("DAHUA_USER", "admin")
        if not password or not user:
            raise CameraError("DAHUA_USER and DAHUA_PASSWORD (or DAHUA_PASS) are required")
        camera = Camera(os.getenv("DAHUA_IP", "192.168.1.50"), user, password)
        try:
            current = {**camera.read("Email"), **camera.read("MotionDetect")}
            if args.show:
                shown = {key: value for key, value in current.items()
                         if key.startswith(("Email.", "MotionDetect[0]."))}
                for key, value in sorted(masked(shown).items()):
                    print(f"{key}={value}")
            else:
                values = desired_config(os.environ, args.motion_level) if args.configure else {
                    "MotionDetect[0].Enable": "false",
                    "MotionDetect[0].EventHandler.MailEnable": "false"}
                if args.configure:
                    for key in ("Email.TlsEnable", "Email.Authentication"):
                        if key in current:
                            values[key] = ("false" if key.endswith("TlsEnable") else
                                           str(bool(os.getenv("SMTP_USER"))).lower())
                apply(camera, values, current)
                print("Configuration applied and read-back verified (password excluded).")
        finally:
            camera.session.close()
        return 0
    except CameraError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
