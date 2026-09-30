"""py2app build script:  .venv/bin/python setup.py py2app"""
from setuptools import setup

OPTIONS = {
    "argv_emulation": False,
    "packages": ["rumps"],
    "includes": ["bridge_core"],
    "plist": {
        "CFBundleName": "FM TCP Bridge",
        "CFBundleDisplayName": "FM TCP Bridge",
        "CFBundleIdentifier": "com.nrgsoft.fmtcpbridge",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSUIElement": True,          # menu bar only, no Dock icon
        "NSHumanReadableCopyright": "© NRG Software",
    },
}

setup(
    name="FM TCP Bridge",
    app=["fm_tcp_bridge_app.py"],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
