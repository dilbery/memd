#!/usr/bin/env python3
"""Generate the public synthetic recall-eval corpus and its golden set.

The private golden set describes a real personal note corpus, so ranking changes
could never be measured on GitHub. This script writes a fictional home-lab
corpus in memd's note format (eval/public/corpus/*.md) and a labelled golden
set (eval/public/golden.jsonl) whose labels come from the generator itself:
every query is written next to the note(s) that answer it.

Deterministic: a fixed seed, no network, no model. The output is committed;
tests/test_public_eval.py regenerates it and fails on any difference, so edit
this script (not the generated files) and re-run it:

    .venv/bin/python eval/public/generate.py

Only placeholder names: hosts gpuhost/vmhost/lapbox/apphost, the domain
home.example.com, addresses 10.10.x.x and 100.64.x.x.

Shapes the corpus deliberately contains, because they are what recall finds
hard (eval/README.md):
  - timelines: many successive dated notes on one topic with near-identical
    wording, where the newest is the answer to "what is the current X";
  - umbrella notes: long multi-section notes with one buried fact per question;
  - superseded notes (never indexed; their replacement is the answer);
  - identifiers, paraphrases, agent-framed tasks, multi-note questions and
    messy verbatim prompts of the kind the recall hook sends.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memd.store import Note, dump_note  # noqa: E402

SEED = 20260928
PROFILE = "amber"
HOSTS = {
    "gpuhost": "10.10.1.20",
    "vmhost": "10.10.1.11",
    "lapbox": "10.10.1.30",
    "apphost": "10.10.1.10",
}


@dataclass
class Doc:
    slug: str
    title: str
    body: str
    host: str = "any"
    importance: int = 3
    tags: list[str] = field(default_factory=list)
    volatility: str | None = None
    observed_at: str | None = None
    superseded_by: str | None = None
    description: str = ""


@dataclass
class Query:
    category: str
    query: str
    gold: list[tuple[str, int]]
    why: str


class Corpus:
    def __init__(self, seed: int = SEED):
        self.rng = random.Random(seed)
        self.docs: dict[str, Doc] = {}
        self.queries: list[Query] = []

    def add(self, doc: Doc) -> str:
        if doc.slug in self.docs:
            raise ValueError(f"duplicate slug {doc.slug}")
        self.docs[doc.slug] = doc
        return doc.slug

    def ask(self, category: str, query: str, *gold: tuple[str, int] | str, why: str) -> None:
        pairs = [(g, 2) if isinstance(g, str) else g for g in gold]
        self.queries.append(Query(category, query, pairs, why))

    def dates(self, n: int, start: str, end: str) -> list[dt.date]:
        """n distinct ascending dates between start and end."""
        a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        days = sorted(self.rng.sample(range((b - a).days + 1), n))
        return [a + dt.timedelta(days=d) for d in days]

    def hexid(self, n: int = 8) -> str:
        return "".join(self.rng.choice("0123456789abcdef") for _ in range(n))


# ---------------------------------------------------------------------------
# Timelines: successive dated notes, newest is the current state.
# ---------------------------------------------------------------------------

def gpu_driver(c: Corpus) -> None:
    rows = [("550.54.14", "12.4", "6.8.9"), ("550.78", "12.4", "6.8.12"),
            ("550.90.07", "12.4", "6.9.3"), ("555.42.02", "12.5", "6.9.7"),
            ("555.58.02", "12.5", "6.9.9"), ("560.28.03", "12.6", "6.10.4"),
            ("560.35.03", "12.6", "6.10.10"), ("565.57.01", "12.7", "6.11.2")]
    triggers = ["the weekly update", "a kernel bump", "an llama-server crash",
                "the monthly patch round", "a DKMS build warning", "a driver release note"]
    extras = ["Nothing else changed.",
              "DKMS needed a manual `dkms autoinstall` after the kernel landed first.",
              "Had to reboot twice: the first boot came up on the fallback framebuffer.",
              "Persistence mode is still enabled through nvidia-persistenced.",
              "The open kernel module is now the default for this card, no flag needed."]
    slugs = []
    for date, (drv, cuda, kernel) in zip(c.dates(len(rows), "2026-01-12", "2026-09-18"), rows):
        slug = c.add(Doc(
            slug=f"gpuhost-nvidia-driver-{date}",
            title=f"gpuhost NVIDIA driver status {date}",
            host="gpuhost", importance=3, tags=["gpu", "nvidia", "gpuhost", "drivers"],
            volatility="state", observed_at=str(date),
            description=f"gpuhost runs NVIDIA {drv} with CUDA {cuda}",
            body=(f"Checked the GPU stack on gpuhost after {c.rng.choice(triggers)}.\n\n"
                  f"- Driver: NVIDIA {drv} (open kernel modules, DKMS)\n"
                  f"- CUDA runtime: {cuda}\n"
                  f"- Kernel: {kernel}\n"
                  f"- `nvidia-smi` shows the card idling at {c.rng.randint(14, 31)} W, "
                  f"{c.rng.randint(34, 47)} C.\n\n"
                  f"{c.rng.choice(extras)} llama-server and the immich ML container both came "
                  "back up without a rebuild. Next check after the next driver release.")))
        slugs.append(slug)
    c.ask("current-state", "what nvidia driver version is gpuhost running right now", slugs[-1],
          why="newest driver status note")
    c.ask("current-state", "current CUDA version on gpuhost", slugs[-1], why="newest driver status note")
    c.ask("real", "whats the latest driver on gpuhost, i need to know before i rebuild llama.cpp",
          slugs[-1], ("llama-cpp-build-flags", 1), why="newest driver note; build flags as context")
    c.ask("identifier", "gpuhost driver 555.42.02", slugs[3], why="the note recording that version")
    c.ask("paraphrase", "which graphics driver did the workstation have back when CUDA 12.4 was installed",
          slugs[0], slugs[1], slugs[2], why="the three CUDA 12.4 driver notes")
    c.gpu_current = slugs[-1]  # type: ignore[attr-defined]


def restic_backups(c: Corpus) -> None:
    n = 8
    dates = c.dates(n, "2026-01-20", "2026-09-21")
    sizes = sorted(c.rng.sample(range(410, 660), n))
    damaged = 4
    slugs = []
    for i, date in enumerate(dates):
        snap = c.hexid()
        if i == damaged:
            check = (f"`restic check` reported `error: pack {c.hexid(12)} damaged`. Ran "
                     "`restic repair packs` and `restic repair snapshots --forget`, then a clean check.")
        else:
            check = "`restic check --read-data-subset=5%` found no errors."
        slug = c.add(Doc(
            slug=f"apphost-restic-backup-check-{date}",
            title=f"apphost restic backup check {date}",
            host="apphost", importance=3, tags=["backup", "restic", "apphost"],
            volatility="state", observed_at=str(date),
            description=f"restic repo {sizes[i]} GiB, last snapshot {snap}",
            body=(f"Backup check for apphost.\n\n"
                  f"- Last snapshot: `{snap}` at {date} 03:{c.rng.randint(10, 40)}\n"
                  f"- Repository size: {sizes[i]} GiB on the NAS share `/mnt/backup/restic`\n"
                  f"- Snapshots kept: {c.rng.randint(24, 31)} (keep-daily 7, keep-weekly 4, "
                  f"keep-monthly 12)\n\n"
                  f"{check} The nightly `restic-backup.service` run by `restic-backup.timer` "
                  "finished without warnings.")))
        slugs.append(slug)
    c.ask("current-state", "latest restic backup status on apphost", slugs[-1], why="newest check")
    c.ask("current-state", "how big is the restic repository at the moment", slugs[-1], why="newest check")
    c.ask("identifier", "restic repair packs damaged pack", slugs[damaged], why="the damaged-pack check")
    c.restic_current = slugs[-1]  # type: ignore[attr-defined]


def dns_upstreams(c: Corpus) -> None:
    states = [
        (["https://doh.example.org/dns-query"], True),
        (["https://doh.example.org/dns-query", "tls://dns1.example.net"], True),
        (["tls://dns1.example.net", "tls://dns2.example.net"], False),
        (["tls://dns1.example.net", "tls://dns2.example.net", "10.10.1.1"], False),
        (["https://doh.example.org/dns-query", "tls://dns2.example.net"], True),
        (["quic://dns3.example.net", "tls://dns2.example.net"], True),
        (["quic://dns3.example.net", "https://doh.example.org/dns-query"], True),
    ]
    reasons = ["latency tests", "a resolver outage", "trying DNS-over-QUIC",
               "the router firmware update", "timeouts on the IoT VLAN"]
    slugs = []
    for date, (ups, dnssec) in zip(c.dates(len(states), "2026-02-02", "2026-09-10"), states):
        slug = c.add(Doc(
            slug=f"dns-resolver-config-{date}",
            title=f"DNS resolver config {date}",
            host="apphost", importance=3, tags=["dns", "adguard", "network"],
            volatility="state", observed_at=str(date),
            body=(f"AdGuard Home on apphost (10.10.1.10:53) after {c.rng.choice(reasons)}.\n\n"
                  "Upstream DNS servers (parallel requests):\n"
                  + "".join(f"- `{u}`\n" for u in ups) +
                  f"\nDNSSEC validation: {'enabled' if dnssec else 'disabled'}. Rewrites for "
                  "`*.home.example.com` still point at 10.10.1.10; DHCP on the router hands out "
                  "10.10.1.10 as the only resolver.")))
        slugs.append(slug)
    c.ask("current-state", "which upstream dns resolvers is adguard using now", slugs[-1], why="newest")
    c.ask("current-state", "is DNSSEC validation currently enabled on the resolver", slugs[-1], why="newest")
    c.dns_current = slugs[-1]  # type: ignore[attr-defined]


def proxmox_updates(c: Corpus) -> None:
    rows = [("8.1.4", "6.5.13-5-pve"), ("8.2.2", "6.8.4-2-pve"), ("8.2.4", "6.8.8-4-pve"),
            ("8.2.7", "6.8.12-2-pve"), ("8.3.0", "6.8.12-4-pve"), ("8.3.2", "6.8.12-5-pve"),
            ("8.3.5", "6.8.12-8-pve"), ("8.4.1", "6.8.12-9-pve")]
    slugs = []
    for date, (pve, kernel) in zip(c.dates(len(rows), "2026-01-08", "2026-09-15"), rows):
        slug = c.add(Doc(
            slug=f"vmhost-proxmox-update-{date}",
            title=f"vmhost Proxmox update {date}",
            host="vmhost", importance=2, tags=["proxmox", "vmhost", "updates"],
            volatility="state", observed_at=str(date),
            body=(f"Updated vmhost with `apt full-upgrade` from the no-subscription repo.\n\n"
                  f"- pve-manager {pve}\n- running kernel {kernel}\n"
                  f"- {c.rng.randint(12, 90)} packages upgraded, reboot {c.rng.choice(['needed', 'done'])}\n\n"
                  "Guests 110 (ci-runner), 120 (sandbox) and 130 (windows-test) came back with "
                  "their autostart order unchanged.")))
        slugs.append(slug)
    c.ask("current-state", "what kernel is vmhost on currently", slugs[-1], why="newest update")
    c.ask("current-state", "latest proxmox version on vmhost", slugs[-1], why="newest update")
    c.ask("identifier", "pve-manager 8.2.4", slugs[2], why="the update to 8.2.4")
    c.pve_current = slugs[-1]  # type: ignore[attr-defined]


def lapbox_disk(c: Corpus) -> None:
    used = [71, 78, 84, 91, 63, 69, 74, 80]
    slugs = []
    for date, pct in zip(c.dates(len(used), "2026-01-15", "2026-09-19"), used):
        action = ("Cleared the pacman cache with `paccache -rk1` and old container images; "
                  "down from 91%." if pct == 63 else
                  "No cleanup needed yet." if pct < 80 else
                  "Getting tight; biggest items are ~/.cache/huggingface and podman images.")
        slug = c.add(Doc(
            slug=f"lapbox-disk-usage-{date}",
            title=f"lapbox disk usage {date}",
            host="lapbox", importance=2, tags=["lapbox", "disk", "storage"],
            volatility="state", observed_at=str(date),
            body=(f"Root filesystem on lapbox (1 TB NVMe, btrfs) at {pct}% used, "
                  f"/home subvolume {c.rng.randint(280, 420)} GB.\n\n{action} "
                  "Snapper keeps 5 hourly and 7 daily snapshots of root.")))
        slugs.append(slug)
    c.ask("current-state", "how full is lapbox's root disk these days", slugs[-1], why="newest")
    c.ask("paraphrase", "when did I last have to free up space on the laptop and how",
          slugs[4], why="the cleanup note")


def caddy_certs(c: Corpus) -> None:
    n = 7
    dates = c.dates(n, "2026-01-25", "2026-09-17")
    failed = 3
    slugs = []
    for i, date in enumerate(dates):
        expiry = date + dt.timedelta(days=c.rng.randint(60, 89))
        if i == failed:
            status = ("Renewal FAILED: `DNS-01 challenge timeout` from the ACME server; the DNS "
                      "provider API token had expired. Rotated the token in "
                      "/srv/compose/caddy/.env and restarted caddy, renewal then succeeded.")
        else:
            status = "Renewal succeeded on the first attempt."
        slug = c.add(Doc(
            slug=f"caddy-tls-renewal-check-{date}",
            title=f"caddy TLS renewal check {date}",
            host="apphost", importance=3, tags=["caddy", "tls", "certificates"],
            volatility="state", observed_at=str(date),
            body=(f"Caddy on apphost holds the wildcard certificate for `*.home.example.com` "
                  f"(DNS-01 challenge). Current certificate expires {expiry}.\n\n{status} "
                  "Checked with `curl -vI https://grafana.home.example.com`.")))
        slugs.append(slug)
    c.ask("current-state", "when does the wildcard cert expire now", slugs[-1], why="newest")
    c.ask("identifier", "DNS-01 challenge timeout caddy", slugs[failed], why="the failed renewal")
    c.ask("real", "caddy keeps failing to renew certs again?? acme errors, what did we do last time",
          slugs[failed], ("caddy-dns-provider-module", 1), why="the failed renewal and its fix")


def llama_model(c: Corpus) -> None:
    rows = [("chat-8b-q6_k.gguf", 8192), ("coder-14b-q4_k_m.gguf", 16384),
            ("coder-14b-q5_k_m.gguf", 16384), ("chat-12b-q5_k_m.gguf", 32768),
            ("coder-32b-q4_k_m.gguf", 16384), ("chat-24b-q4_k_m.gguf", 32768),
            ("coder-30b-a3b-q4_k_m.gguf", 65536)]
    slugs = []
    for date, (model, ctx) in zip(c.dates(len(rows), "2026-02-10", "2026-09-20"), rows):
        slug = c.add(Doc(
            slug=f"gpuhost-llama-server-model-{date}",
            title=f"gpuhost llama-server model {date}",
            host="gpuhost", importance=3, tags=["llm", "llama-server", "gpuhost"],
            volatility="state", observed_at=str(date),
            body=(f"llama-server on gpuhost (port 8080) now serves `{model}` with "
                  f"`--ctx-size {ctx}` and `-ngl 99`. Started by the user unit "
                  f"`llama-server.service`; about {c.rng.randint(38, 110)} tokens/s generation "
                  "on a short prompt.")))
        slugs.append(slug)
    c.ask("current-state", "which model is llama-server serving at the moment", slugs[-1], why="newest")
    c.ask("real", "what context size is the local llm on gpuhost using now, my prompt got cut off",
          slugs[-1], why="newest")
    c.llama_current = slugs[-1]  # type: ignore[attr-defined]


def lapbox_updates(c: Corpus) -> None:
    lts = ["6.6.72", "6.6.75", "6.6.79", "6.6.83", "6.12.21", "6.12.25", "6.12.30",
           "6.12.34", "6.12.39", "6.12.44"]
    issues = ["No issues.", "mesa update needed a reboot for the compositor.",
              "pacnew for /etc/pacman.conf merged.", "firmware update for the dock via fwupd.",
              "python rebuild broke two AUR packages; rebuilt with paru."]
    slugs = []
    for date, ver in zip(c.dates(len(lts), "2026-01-05", "2026-09-22"), lts):
        slug = c.add(Doc(
            slug=f"lapbox-system-update-{date}",
            title=f"lapbox system update {date}",
            host="lapbox", importance=2, tags=["lapbox", "updates", "pacman"],
            volatility="volatile", observed_at=str(date),
            body=(f"`pacman -Syu` on lapbox: {c.rng.randint(40, 260)} packages upgraded, "
                  f"linux-lts {ver}. {c.rng.choice(issues)} `sbctl verify` clean afterwards.")))
        slugs.append(slug)
    c.ask("current-state", "what linux-lts version is lapbox on now", slugs[-1], why="newest")
    c.ask("identifier", "linux-lts 6.12.21 lapbox", slugs[4], why="first 6.12 update")


def tank_usage(c: Corpus) -> None:
    used = [9.1, 9.6, 10.2, 10.9, 11.4, 12.3, 12.8, 13.5]
    slugs = []
    for date, tib in zip(c.dates(len(used), "2026-01-18", "2026-09-14"), used):
        pct = round(tib / 14.5 * 100)
        slug = c.add(Doc(
            slug=f"tank-pool-usage-{date}",
            title=f"tank pool usage {date}",
            host="apphost", importance=2, tags=["zfs", "storage", "capacity"],
            volatility="state", observed_at=str(date),
            body=(f"`zpool list tank`: {tib} TiB allocated of 14.5 TiB usable ({pct}%), "
                  f"fragmentation {c.rng.randint(3, 14)}%. Largest datasets: tank/media, "
                  "tank/immich, tank/backup. "
                  + ("Above the 80% line where ZFS slows down; plan the next disk purchase."
                     if pct >= 80 else "Fine for now."))))
        slugs.append(slug)
    c.ask("current-state", "how full is the zfs pool on apphost right now", slugs[-1], why="newest")


def speedtest(c: Corpus) -> None:
    rows = [(248, 41, 11), (251, 43, 10), (94, 38, 29), (247, 42, 11), (495, 48, 9),
            (502, 51, 9), (488, 50, 10), (910, 96, 7)]
    notes = {2: "Evening dip; the ISP confirmed a congested node, fixed two days later.",
             4: "Plan upgraded to 500/50.", 7: "Plan upgraded to 1000/100; router still keeps up."}
    slugs = []
    for i, (date, (down, up, ping)) in enumerate(
            zip(c.dates(len(rows), "2026-01-22", "2026-09-19"), rows)):
        slug = c.add(Doc(
            slug=f"internet-speed-test-{date}",
            title=f"internet speed test {date}",
            importance=1, tags=["network", "isp", "speedtest"],
            volatility="volatile", observed_at=str(date),
            body=(f"Speed test from apphost over the wired uplink: {down} Mbit/s down, {up} "
                  f"Mbit/s up, {ping} ms ping. " + notes.get(i, "Normal."))))
        slugs.append(slug)
    c.ask("current-state", "what internet speed do we get these days", slugs[-1], why="newest")
    c.ask("paraphrase", "when was the connection slow in the evenings and what was the cause",
          slugs[2], why="the congested-node note")


DEPLOYS = {
    "grafana": ("grafana/grafana", ["11.0.0", "11.1.0", "11.1.4", "11.2.0", "11.3.1"]),
    "immich": ("ghcr.io/immich-app/immich-server", ["v1.118.2", "v1.119.1", "v1.120.2",
                                                    "v1.121.0", "v1.123.0"]),
    "paperless": ("ghcr.io/paperless-ngx/paperless-ngx", ["2.11.6", "2.12.1", "2.13.5", "2.14.7"]),
    "forge": ("forgejo/forgejo", ["8.0.3", "9.0.1", "9.0.3", "10.0.0"]),
    "home-assistant": ("ghcr.io/home-assistant/home-assistant",
                       ["2026.3.4", "2026.4.2", "2026.5.3", "2026.6.1", "2026.7.2", "2026.8.3"]),
    "jellyfin": ("jellyfin/jellyfin", ["10.9.11", "10.10.1", "10.10.3", "10.10.6"]),
}


def deploys(c: Corpus) -> None:
    latest: dict[str, str] = {}
    by_version: dict[tuple[str, str], str] = {}
    for svc, (image, versions) in DEPLOYS.items():
        dates = c.dates(len(versions), "2026-01-10", "2026-09-20")
        prev = None
        for date, ver in zip(dates, versions):
            rollback = ""
            if svc == "immich" and ver == "v1.121.0":
                rollback = ("\n\nRolled back to v1.120.2 the same evening: the machine-learning "
                            "container on gpuhost was still on the old version and face "
                            "detection jobs failed. Upgraded both together next time.")
            slug = c.add(Doc(
                slug=f"deploy-{svc}-{ver.lstrip('v').replace('.', '-')}-{date}",
                title=f"Deploy {svc} {ver} to apphost ({date})",
                host="apphost", importance=2, tags=["deploy", svc, "apphost"],
                volatility="volatile", observed_at=str(date),
                body=(f"Bumped `{image}:{ver}` in /srv/compose/{svc}/compose.yaml"
                      + (f" (was {prev})" if prev else "") +
                      ", then `docker compose pull && docker compose up -d`. "
                      f"Health check green after {c.rng.randint(20, 95)} s; "
                      f"{c.rng.choice(['release notes had no breaking changes', 'one config key renamed, fixed in .env', 'database migration ran on start', 'dashboards and users intact'])}."
                      f"{rollback}")))
            prev = ver
            latest[svc] = slug
            by_version[(svc, ver)] = slug
    c.ask("current-state", "which grafana version is running on apphost now", latest["grafana"], why="newest")
    c.ask("current-state", "latest home assistant version we deployed", latest["home-assistant"], why="newest")
    c.ask("current-state", "current immich version on apphost", latest["immich"], why="newest")
    c.ask("current-state", "what forgejo version is the git server on at the moment", latest["forge"],
          why="newest")
    c.ask("identifier", "immich v1.121.0 rolled back", by_version[("immich", "v1.121.0")],
          why="the rollback note")
    c.ask("identifier", "paperless-ngx 2.13.5", by_version[("paperless", "2.13.5")], why="that deploy")
    c.ask("paraphrase", "why did the photo library upgrade get reverted",
          by_version[("immich", "v1.121.0")], ("immich-ml-on-gpuhost", 1), why="rollback note")
    c.ask("agent-framed", "I'm going to upgrade jellyfin on apphost. Which version is deployed right "
          "now and where is its compose file?", latest["jellyfin"], ("apphost-services-runbook", 1),
          why="newest jellyfin deploy; runbook for layout")
    c.deploy_latest = latest  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Standalone fact notes (how-tos, incidents, decisions).
# ---------------------------------------------------------------------------

FACTS: list[dict] = [
    dict(slug="vaultwarden-admin-token", host="apphost", imp=4, tags=["vaultwarden", "security"],
         title="Vaultwarden admin token rotation",
         body="The Vaultwarden admin page token is stored as an argon2 PHC hash in "
              "`/srv/compose/vaultwarden/.env` as `ADMIN_TOKEN`. To rotate: run "
              "`docker exec -it vaultwarden /vaultwarden hash`, paste the output into the .env "
              "(single-quote it, the `$` signs break compose interpolation otherwise) and "
              "`docker compose up -d`. `SIGNUPS_ALLOWED=false`; new family accounts are invited "
              "from the admin page."),
    dict(slug="vmhost-nfs-stale-file-handle", host="vmhost", imp=3, tags=["nfs", "storage"],
         title="Stale file handle on vmhost /mnt/media after apphost reboot",
         body="vmhost mounts `apphost:/srv/media` over NFSv4 at /mnt/media. After apphost "
              "reboots, every access fails with `Stale file handle`. Fix: the fstab entry now uses "
              "`soft,timeo=150,x-systemd.automount,x-systemd.idle-timeout=600` so the automount "
              "unit remounts on next access. Manual recovery: `umount -l /mnt/media && mount "
              "/mnt/media`."),
    dict(slug="zfs-scrub-schedule", host="apphost", imp=3, tags=["zfs", "storage", "maintenance"],
         title="ZFS scrub schedule for tank",
         body="Pool `tank` on apphost is raidz1 over three 8 TB disks. A scrub runs on the first "
              "Sunday of each month via `zfs-scrub-monthly@tank.timer` (from zfsutils). Results "
              "land in `zpool status tank`; ZED mails errors to the admin alias. Last scrubs take "
              "about 9 hours."),
    dict(slug="ups-nut-shutdown", host="apphost", imp=4, tags=["ups", "power", "nut"],
         title="UPS and NUT shutdown sequence",
         body="The UPS is attached to apphost by USB and monitored by NUT (`upsmon` as primary). "
              "vmhost and gpuhost run `upsmon` as secondaries against apphost on port 3493. "
              "When battery.charge drops below 30% the secondaries shut down first, apphost "
              "last. All three have \"restore on AC power loss\" set to *last state* in firmware; "
              "gpuhost stays off and is woken later with wake-on-lan."),
    dict(slug="ssh-key-policy", host="any", imp=4, tags=["ssh", "security", "policy"],
         title="SSH key policy for lab hosts",
         body="Only ed25519 keys. `authorized_keys` on every host is managed by the ansible role "
              "`base_ssh`; never edit it by hand, it is overwritten on the next run. "
              "`PasswordAuthentication no` and `PermitRootLogin prohibit-password` everywhere. "
              "New machines get the role applied before they join the inventory."),
    dict(slug="homelab-ansible-repo", host="lapbox", imp=4, tags=["ansible", "automation"],
         title="Ansible repo and how to run it",
         body="The playbooks live in `~/src/homelab-ansible` on lapbox. Inventory `hosts.ini` has "
              "groups `[docker]` (apphost), `[gpu]` (gpuhost), `[pve]` (vmhost) and "
              "`[workstation]` (lapbox). Always run `ansible-playbook site.yml -l <host> --check "
              "--diff` first and read the diff; the `docker` role restarts the daemon when "
              "daemon.json changes, which bounces every container."),
    dict(slug="docker-log-rotation", host="apphost", imp=3, tags=["docker", "logging", "disk"],
         title="Docker log rotation on apphost",
         body="/var/lib/docker filled the root filesystem in March because containers logged "
              "with the unbounded json-file driver. `/etc/docker/daemon.json` now sets "
              "`\"log-driver\": \"local\"` with `\"max-size\": \"20m\"` and `\"max-file\": \"5\"`. "
              "Existing containers only pick it up when recreated (`docker compose up -d "
              "--force-recreate`)."),
    dict(slug="grafana-admin-password-reset", host="apphost", imp=2, tags=["grafana", "howto"],
         title="Reset the Grafana admin password",
         body="`docker exec -it grafana grafana cli admin reset-admin-password <new>` inside the "
              "running container. The login is `admin`; the password is not in the .env because "
              "the provisioning only sets it on first start."),
    dict(slug="gpuhost-suspend-resume-nvidia", host="gpuhost", imp=3,
         tags=["gpuhost", "nvidia", "suspend"],
         title="gpuhost black screen after resume from suspend",
         body="Resuming gpuhost from suspend gave a black screen with the NVIDIA card. Fix: "
              "`options nvidia NVreg_PreserveVideoMemoryAllocations=1` in "
              "/etc/modprobe.d/nvidia-power.conf, and enable `nvidia-suspend.service`, "
              "`nvidia-resume.service` and `nvidia-hibernate.service`. Rebuild the initramfs "
              "afterwards."),
    dict(slug="gpuhost-wake-on-lan", host="gpuhost", imp=3, tags=["gpuhost", "wol", "power"],
         title="Wake gpuhost with wake-on-lan",
         body="gpuhost sleeps when idle. Wake it from apphost with `wakeonlan 02:00:00:00:00:20` "
              "(onboard NIC, not the 10GbE card). Needs \"Power On By PCI-E\" enabled in firmware "
              "and `ethtool -s eno1 wol g`, persisted by a systemd-networkd `.link` file."),
    dict(slug="tailscale-subnet-router", host="apphost", imp=4, tags=["tailscale", "vpn", "remote"],
         title="Remote access through the tailscale subnet router",
         body="apphost is the tailscale subnet router: `tailscale up --advertise-routes="
              "10.10.1.0/24 --accept-dns=false`, route approved in the admin console. From "
              "outside the house every 10.10.1.x address works directly; MagicDNS names are "
              "disabled so `*.home.example.com` still resolves through AdGuard over the tunnel."),
    dict(slug="forge-ssh-port", host="apphost", imp=3, tags=["forge", "git", "ssh"],
         title="Git server: SSH on port 2200",
         body="The forge runs on apphost; web UI at `https://git.home.example.com` (caddy -> "
              "port 3000), git over SSH on port 2200 because 22 is the host's own sshd. Clone "
              "with `ssh://git@git.home.example.com:2200/<owner>/<repo>.git`, or add a `Host "
              "git.home.example.com` block with `Port 2200` to ~/.ssh/config."),
    dict(slug="mosquitto-auth", host="apphost", imp=3, tags=["mqtt", "mosquitto", "iot"],
         title="Mosquitto broker authentication",
         body="MQTT broker on apphost port 1883. `allow_anonymous false`; users in "
              "`/srv/compose/mosquitto/config/passwd`, managed with `mosquitto_passwd`. The IoT "
              "VLAN may reach only this port. zigbee2mqtt and Home Assistant each have their own "
              "user."),
    dict(slug="apphost-sdc-reallocated-sectors", host="apphost", imp=4,
         tags=["disk", "smart", "zfs", "hardware"],
         title="apphost /dev/sdc reallocated sectors rising",
         body="smartd warned about `/dev/sdc` (one of the three `tank` disks): "
              "Reallocated_Sector_Ct went from 8 to 24 in two weeks, Current_Pending_Sector 2. "
              "The pool is still ONLINE with no read errors. Replacement 8 TB disk ordered; "
              "swap with `zpool replace tank <old-id> <new-id>` and let it resilver."),
    dict(slug="apphost-systemd-timers", host="apphost", imp=3, tags=["systemd", "scheduling"],
         title="Scheduled jobs on apphost are systemd timers",
         body="All scheduled jobs on apphost moved from root's crontab to systemd timers in "
              "February: restic-backup, pg-dump, rclone-offsite, zfs scrub and docker image "
              "prune. List them with `systemctl list-timers`; units live in /etc/systemd/system "
              "and are deployed by ansible. The crontab is empty on purpose."),
    dict(slug="llama-cpp-build-flags", host="gpuhost", imp=3, tags=["llm", "llama.cpp", "cuda"],
         title="Building llama.cpp on gpuhost",
         body="`cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -DGGML_NATIVE=ON && "
              "cmake --build build -j` in ~/src/llama.cpp. The CUDA toolkit must match the "
              "driver's CUDA version or the build links fine but fails at runtime with "
              "`CUDA driver version is insufficient`."),
    dict(slug="python-uv-policy", host="any", imp=3, tags=["python", "tooling", "policy"],
         title="Python tooling: use uv everywhere",
         body="New Python projects use uv: `uv venv --python 3.13 .venv`, dependencies in "
              "pyproject.toml, `uv pip install -e '.[dev]'`. No global pip installs, no conda. "
              "CLI tools go in with `uv tool install`."),
    dict(slug="docker-firewall-docker-user", host="apphost", imp=4, tags=["firewall", "docker", "nftables"],
         title="Docker publishes ports past the firewall",
         body="Published container ports on apphost were reachable from the guest VLAN although "
              "the nftables input chain drops them: docker inserts its own forward rules. Filter "
              "in the `DOCKER-USER` chain instead (allow 10.10.1.0/24 and the tailscale range, "
              "drop the rest), or publish on 127.0.0.1 and let caddy proxy."),
    dict(slug="lapbox-printing", host="lapbox", imp=1, tags=["printer", "cups"],
         title="Printing from lapbox",
         body="The laser printer is at 10.10.1.40 and speaks IPP Everywhere, so CUPS needs no "
              "driver: `lpadmin -p laser -E -v ipp://10.10.1.40/ipp/print -m everywhere`. "
              "Duplex default set in the web UI on localhost:631."),
    dict(slug="syncthing-folders", host="lapbox", imp=2, tags=["syncthing", "sync"],
         title="Syncthing folders between lapbox and gpuhost",
         body="Syncthing syncs `~/notes`, `~/Documents` and `~/src/scratch` between lapbox and "
              "gpuhost. `.stignore` excludes `.venv`, `node_modules` and `*.gguf`. Model files "
              "are copied by hand because they are too large."),
    dict(slug="immich-ml-on-gpuhost", host="gpuhost", imp=3, tags=["immich", "gpu", "photos"],
         title="Immich machine learning runs on gpuhost",
         body="The immich server stays on apphost, but the machine-learning container (face "
              "detection, CLIP search) runs on gpuhost to use its GPU: "
              "`IMMICH_MACHINE_LEARNING_URL=http://10.10.1.20:3003` in the server's .env. "
              "When gpuhost sleeps, smart search falls back to slow CPU or times out; upgrade "
              "both containers together."),
    dict(slug="lapbox-linux-lts-iwlwifi", host="lapbox", imp=3, tags=["lapbox", "kernel", "wifi"],
         title="lapbox runs linux-lts because of iwlwifi crashes",
         body="The mainline kernel crashed the Wi-Fi firmware on lapbox (`iwlwifi: Failed to "
              "run INIT ucode: -110`) after resume. Switched to linux-lts, which does not. Keep "
              "linux-lts as the default boot entry until mainline has been tested again."),
    dict(slug="rclone-offsite-copy", host="apphost", imp=4, tags=["backup", "rclone", "offsite"],
         title="Offsite copy of the restic repository",
         body="Every Sunday night `rclone-offsite.timer` runs `rclone sync /mnt/backup/restic "
              "offsite:homelab-offsite --bwlimit 4M --fast-list` to object storage. The bucket "
              "has versioning and a 30-day object lock, so a ransomware sync cannot destroy "
              "older copies."),
    dict(slug="vmhost-vm-inventory", host="vmhost", imp=3, tags=["proxmox", "vm", "vmhost"],
         title="vmhost VM inventory",
         body="Proxmox guests on vmhost: 110 `ci-runner` (Debian 12, 4 vCPU, 8 GB, runs the "
              "forge actions runner), 120 `sandbox` (throwaway experiments, snapshots before "
              "every change), 130 `windows-test` (off unless needed). Storage is local-lvm; "
              "backups via vzdump to the NAS weekly."),
    dict(slug="forge-actions-runner", host="vmhost", imp=3, tags=["ci", "forge", "runner"],
         title="CI runner registration",
         body="The forge actions runner lives in VM 110 (`ci-runner`) on vmhost, registered "
              "with labels `docker:docker://node:20-bookworm` and `ubuntu-latest:docker://"
              "node:20-bookworm`. If jobs sit in *queued*, the runner usually lost its token "
              "after a forge upgrade: re-register with `forgejo-runner register` using a new "
              "token from site administration."),
    dict(slug="caddy-dns-provider-module", host="apphost", imp=3, tags=["caddy", "tls", "build"],
         title="Caddy is built with a DNS provider module",
         body="The stock caddy image cannot do DNS-01, so apphost builds its own with `xcaddy "
              "build --with github.com/caddy-dns/<provider>` in /srv/compose/caddy/Dockerfile. "
              "The DNS provider API token is in /srv/compose/caddy/.env and expires yearly."),
    dict(slug="zigbee-coordinator-path", host="apphost", imp=3, tags=["zigbee", "iot", "usb"],
         title="Zigbee coordinator device path",
         body="The Zigbee USB coordinator is plugged into apphost (on a USB extension cable, "
              "away from the USB3 ports' interference). zigbee2mqtt uses the stable path "
              "`/dev/serial/by-id/usb-zigbee-coordinator-if00-port0`, never /dev/ttyUSB0 which "
              "changes between boots."),
    dict(slug="gpuhost-fan-curve", host="gpuhost", imp=2, tags=["gpuhost", "fans", "noise"],
         title="gpuhost fan curve and noise",
         body="gpuhost was loud under sustained GPU load. Case fans follow `fancontrol` "
              "(/etc/fancontrol, hwmon of the motherboard's nct6799 chip) with a flat curve up "
              "to 60 C. GPU fans are left on the vendor curve; the power limit does more for "
              "noise than any curve."),
    dict(slug="chrony-time-sync", host="apphost", imp=2, tags=["ntp", "chrony", "time"],
         title="Time sync: apphost is the LAN NTP server",
         body="All hosts run chrony. apphost syncs from public pool servers and serves NTP to "
              "192.168.0.0/16 (`allow 192.168.0.0/16` in chrony.conf); the others use only "
              "`server 10.10.1.10 iburst`. The IoT VLAN gets it via DHCP option 42."),
    dict(slug="compose-layout-convention", host="apphost", imp=4, tags=["docker", "compose", "convention"],
         title="Compose layout convention on apphost",
         body="Every stack lives in `/srv/compose/<name>/compose.yaml` with its `.env` next to "
              "it (mode 600, not in git) and persistent data under `/srv/data/<name>`. Update "
              "with `docker compose pull && docker compose up -d` from that directory. Add the "
              "data path to the restic include list when creating a new stack."),
    dict(slug="postgres-shared-dumps", host="apphost", imp=3, tags=["postgres", "backup", "database"],
         title="Shared Postgres and nightly dumps",
         body="One `postgres:16` container named `db` on apphost serves paperless and the forge. "
              "`pg-dump.timer` runs `pg_dumpall` at 02:30 into /srv/backup/pg (keep 7), which "
              "restic picks up at 03:00. Restic never reads the live data directory."),
    dict(slug="lapbox-zram-swap", host="lapbox", imp=1, tags=["lapbox", "memory", "swap"],
         title="lapbox zram swap",
         body="No swap partition on lapbox; `zram-generator` provides 8 GB of zstd-compressed "
              "swap (`/etc/systemd/zram-generator.conf`: `zram-size = min(ram / 2, 8192)`)."),
    dict(slug="lapbox-secure-boot-sbctl", host="lapbox", imp=3, tags=["lapbox", "secureboot"],
         title="Secure Boot on lapbox with sbctl",
         body="lapbox boots with Secure Boot using our own keys enrolled by sbctl. The pacman "
              "hook re-signs kernels, but after any bootloader or kernel change run `sbctl "
              "verify`; an unsigned file means the next boot stops at the firmware."),
    dict(slug="apphost-ram-arc-limit", host="apphost", imp=3, tags=["zfs", "memory", "apphost"],
         title="apphost RAM upgrade and ZFS ARC limit",
         body="apphost went from 32 GB to 64 GB of RAM in May. The ZFS ARC is capped at 16 GB "
              "with `options zfs zfs_arc_max=17179869184` in /etc/modprobe.d/zfs.conf so "
              "containers keep headroom; `arc_summary` shows the hit rate stays above 95%."),
    dict(slug="ssh-proxyjump-config", host="lapbox", imp=2, tags=["ssh", "lapbox"],
         title="SSH to vmhost guests through a jump host",
         body="The Proxmox guests are on an internal bridge. From lapbox, `~/.ssh/config` has "
              "`Host ci-runner sandbox` with `ProxyJump vmhost`, so `ssh sandbox` works from "
              "anywhere on the LAN or tailscale."),
    dict(slug="uptime-kuma-status", host="apphost", imp=2, tags=["monitoring", "uptime-kuma"],
         title="Uptime Kuma status page",
         body="Uptime Kuma on apphost checks every web service over HTTPS every 60 s. The status "
              "page slug is `hl`. gpuhost is checked with ICMP only and marked as allowed to "
              "be down because it sleeps."),
]


def facts(c: Corpus) -> None:
    for f in FACTS:
        c.add(Doc(slug=f["slug"], title=f["title"], body=f["body"], host=f["host"],
                  importance=f["imp"], tags=f["tags"],
                  volatility="durable" if "policy" in f["tags"] or "convention" in f["tags"] else None,
                  description=f["body"].split(". ")[0][:120]))
    A = c.ask
    A("identifier", "vaultwarden ADMIN_TOKEN argon2 hash", "vaultwarden-admin-token", why="how-to")
    A("paraphrase", "how do I change the password manager's admin password", "vaultwarden-admin-token",
      why="token rotation is the admin password")
    A("identifier", "Stale file handle /mnt/media vmhost", "vmhost-nfs-stale-file-handle", why="incident")
    A("real", "media mount on vmhost is broken again after i rebooted apphost, stale handle errors",
      "vmhost-nfs-stale-file-handle", why="incident")
    A("identifier", "zfs-scrub-monthly@tank.timer", "zfs-scrub-schedule", why="unit name")
    A("paraphrase", "how often does the storage pool get checked for bit rot", "zfs-scrub-schedule",
      why="scrub schedule")
    A("paraphrase", "what happens to the servers during a power cut", "ups-nut-shutdown",
      why="shutdown sequence")
    A("identifier", "upsmon secondary port 3493", "ups-nut-shutdown", why="port")
    A("paraphrase", "which kind of ssh key should I generate for the lab machines", "ssh-key-policy",
      why="policy")
    A("agent-framed", "I'm adding a new VM to the lab. How should I set up SSH access so it matches "
      "the other hosts?", "ssh-key-policy", ("homelab-ansible-repo", 1), why="policy + ansible role")
    A("agent-framed", "Before I run the ansible playbook against apphost, is there anything I need "
      "to check first?", "homelab-ansible-repo", why="--check --diff and daemon restart warning")
    A("identifier", "homelab-ansible site.yml inventory", "homelab-ansible-repo", why="repo")
    A("real", "apphost root filesystem full again? pretty sure its docker logs",
      "docker-log-rotation", why="log rotation incident")
    A("identifier", "daemon.json max-size log-driver local", "docker-log-rotation", why="settings")
    A("paraphrase", "I forgot the login for the metrics dashboards", "grafana-admin-password-reset",
      why="grafana reset")
    A("identifier", "NVreg_PreserveVideoMemoryAllocations", "gpuhost-suspend-resume-nvidia", why="option")
    A("real", "gpuhost black screen after waking from sleep again ugh", "gpuhost-suspend-resume-nvidia",
      why="incident")
    A("paraphrase", "how do I turn the workstation on remotely when it's asleep", "gpuhost-wake-on-lan",
      why="wol")
    A("paraphrase", "how can I reach the home network when I'm travelling", "tailscale-subnet-router",
      why="remote access")
    A("identifier", "git.home.example.com port 2200", "forge-ssh-port", why="ssh port")
    A("real", "git clone over ssh to the forge hangs, what port is it on", "forge-ssh-port", why="port")
    A("identifier", "mosquitto passwd file", "mosquitto-auth", why="path")
    A("identifier", "Reallocated_Sector_Ct /dev/sdc", "apphost-sdc-reallocated-sectors", why="smart")
    A("real", "is one of the nas disks dying? got a smartd email", "apphost-sdc-reallocated-sectors",
      why="smart warning")
    A("paraphrase", "where are the recurring jobs on the docker server defined these days",
      "apphost-systemd-timers", why="timers, not cron")
    A("identifier", "CMAKE_CUDA_ARCHITECTURES llama.cpp", "llama-cpp-build-flags", why="build flags")
    A("agent-framed", "I'm setting up a new Python project on lapbox; which tooling do we use for "
      "virtual environments and dependencies?", "python-uv-policy", why="policy")
    A("paraphrase", "why can people on the guest wifi reach container ports even though the firewall "
      "blocks them", "docker-firewall-docker-user", why="DOCKER-USER")
    A("paraphrase", "how do I print something from the laptop", "lapbox-printing", why="printing")
    A("real", "which folders does syncthing sync again", "syncthing-folders", why="folders")
    A("identifier", "IMMICH_MACHINE_LEARNING_URL", "immich-ml-on-gpuhost", why="env var")
    A("paraphrase", "which machine does the face recognition for photos run on", "immich-ml-on-gpuhost",
      why="ML on gpuhost")
    A("identifier", "iwlwifi Failed to run INIT ucode -110", "lapbox-linux-lts-iwlwifi", why="error")
    A("paraphrase", "is there a copy of the backups outside the house", "rclone-offsite-copy",
      why="offsite copy")
    A("paraphrase", "which virtual machine runs the CI jobs", "vmhost-vm-inventory",
      ("forge-actions-runner", 2), why="inventory and runner note both answer")
    A("agent-framed", "CI jobs have been stuck in queued for an hour. Where does the runner live and "
      "how do I re-register it?", "forge-actions-runner", ("vmhost-vm-inventory", 1), why="runner")
    A("identifier", "xcaddy dns provider module", "caddy-dns-provider-module", why="build")
    A("identifier", "/dev/serial/by-id zigbee coordinator", "zigbee-coordinator-path", why="path")
    A("real", "gpuhost is way too loud when the gpu is busy", "gpuhost-fan-curve",
      ("gpuhost-setup-notes", 1), why="fan curve; power limit in setup notes")
    A("paraphrase", "which box hands out the time to the rest of the network", "chrony-time-sync",
      why="ntp server")
    A("agent-framed", "I need to add a new self-hosted service on apphost. Where should the compose "
      "file and its data go, and what else do I have to remember?", "compose-layout-convention",
      why="layout convention")
    A("paraphrase", "how are the databases backed up before restic runs", "postgres-shared-dumps",
      why="pg_dumpall")
    A("identifier", "zram-generator zram-size", "lapbox-zram-swap", why="config")
    A("identifier", "sbctl verify", "lapbox-secure-boot-sbctl", why="secure boot")
    A("identifier", "zfs_arc_max", "apphost-ram-arc-limit", why="modprobe option")
    A("paraphrase", "how much memory does the storage cache get on the docker server",
      "apphost-ram-arc-limit", why="ARC limit")
    A("real", "how do i ssh into the sandbox vm from my laptop", "ssh-proxyjump-config",
      ("vmhost-vm-inventory", 1), why="ProxyJump")


# ---------------------------------------------------------------------------
# Umbrella notes: long, many sections, one buried fact per question.
# ---------------------------------------------------------------------------

def _section(name: str, lines: list[str]) -> str:
    return f"## {name}\n\n" + "\n".join(lines) + "\n"


def _changelog(c: Corpus, entries: list[str], start: str = "2025-11-01",
               end: str = "2026-09-20") -> tuple[str, list[str]]:
    """A dated "Changelog" section: the kind of history that pads umbrella notes."""
    dates = c.dates(len(entries), start, end)
    return ("Changelog", [f"- {d}: {e}" for d, e in zip(dates, entries)])


def apphost_runbook(c: Corpus) -> None:
    services = [
        ("caddy", 443, "*", "Reverse proxy for every web service; config in `Caddyfile`, reload "
         "with `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`."),
        ("grafana", 3000, "grafana", "Dashboards are provisioned from `/srv/data/grafana/"
         "provisioning`; edits in the UI are lost on restart unless exported."),
        ("prometheus", 9090, "prom", "Scrape config is templated by ansible; see the monitoring "
         "reference for jobs and retention."),
        ("immich", 2283, "photos", "Uploads land in `/srv/data/immich/library`; the database is "
         "its own `immich-db` container, not the shared postgres."),
        ("paperless", 8000, "docs", "OCR languages are deu+eng (`PAPERLESS_OCR_LANGUAGE=deu+eng`); "
         "the consume folder is `/srv/scan/inbox`, fed by the scanner's SMB share."),
        ("jellyfin", 8096, "media", "Hardware transcoding is disabled: apphost exposes no "
         "`/dev/dri` to the container, so 4K files are pre-transcoded on gpuhost with ffmpeg."),
        ("vaultwarden", 8081, "vault", "WebSocket notifications need the `/notifications/hub` route "
         "in the Caddyfile, otherwise clients only sync every few minutes."),
        ("forge", 3001, "git", "LFS objects live in `/srv/forge/lfs`, outside the data volume, and "
         "are excluded from restic because they can be re-fetched."),
        ("home-assistant", 8123, "home", "Runs with `network_mode: host` for mDNS discovery; its "
         "port is therefore not published through compose."),
        ("adguard", 53, "dns", "Admin UI on port 3080 behind caddy; the DNS port binds to "
         "10.10.1.10 only, not 0.0.0.0, so systemd-resolved can keep 127.0.0.53."),
        ("mosquitto", 1883, "-", "Plain MQTT on the LAN and IoT VLAN; no TLS listener yet."),
        ("uptime-kuma", 3002, "status", "Notifications go to the same ntfy topic as alertmanager."),
        ("ntfy", 8090, "ntfy", "Topics are protected with access tokens; the phone app "
         "subscribes over the tailscale route."),
        ("db", 5432, "-", "Shared postgres:16 for paperless and the forge; see the dumps note."),
    ]
    intro = ("Runbook for every container stack on apphost (10.10.1.10). Each stack follows "
             "the compose layout convention. Start/stop order matters only for `db` (first) and "
             "`caddy` (last). This note is the umbrella; the per-incident notes hold history.\n")
    parts = [intro]
    for name, port, sub, quirk in services:
        lines = [f"- Compose: `/srv/compose/{name}/compose.yaml`, data `/srv/data/{name}`",
                 f"- Port: {port}" + (f", public name `{sub}.home.example.com`" if sub not in "-*" else ""),
                 f"- Restart policy: `unless-stopped`; image pinned by tag, updated by hand",
                 f"- Backup: {'excluded' if name in ('prometheus', 'ntfy') else 'included in the nightly restic run'}",
                 "",
                 quirk]
        parts.append(_section(name, lines))
    slug = c.add(Doc(slug="apphost-services-runbook", title="apphost services runbook",
                     body="\n".join(parts), host="apphost", importance=4,
                     tags=["apphost", "runbook", "docker", "services"], volatility="durable",
                     description="Every container stack on apphost: paths, ports, quirks"))
    A = c.ask
    A("paraphrase", "what language does the document scanner OCR use", slug, why="paperless quirk")
    A("identifier", "PAPERLESS_OCR_LANGUAGE", slug, why="paperless quirk")
    A("current-state", "is hardware transcoding currently enabled in jellyfin", slug, why="jellyfin quirk")
    A("real", "vaultwarden clients not syncing instantly, something about websockets?", slug,
      why="vaultwarden quirk")
    A("paraphrase", "are the large git files included in the backup", slug, why="forge LFS quirk")
    A("agent-framed", "I want to change the home assistant port mapping in its compose file. Anything "
      "unusual about how that container is networked?", slug, why="host networking quirk")


def monitoring_reference(c: Corpus) -> None:
    sections = [
        ("Overview", ["Prometheus, Alertmanager, Loki and Grafana all run on apphost. Exporters run "
                      "on every host. Dashboards: node, docker, zfs, gpu, blackbox."]),
        ("Prometheus", [f"- scrape interval 30s, evaluation interval 30s",
                        "- retention: `--storage.tsdb.retention.time=45d` (raised from 15d in June)",
                        "- TSDB on `/srv/data/prometheus`, about 18 GB"]),
        ("Scrape jobs", ["- `node`: all four hosts on :9100",
                         "- `cadvisor`: apphost :8088",
                         "- `nvidia`: gpuhost :9835 (nvidia_gpu_exporter)",
                         "- `zfs`: apphost :9134",
                         "- `blackbox`: HTTPS probes of every `*.home.example.com` name"]),
        ("Exporters per host", [
            "- apphost: node_exporter, cadvisor, zfs_exporter, smartctl_exporter, promtail",
            "- vmhost: node_exporter, pve_exporter (API token `monitoring@pve!prom`), promtail",
            "- gpuhost: node_exporter, nvidia_gpu_exporter, promtail; scraped with "
            "`honor_labels` off and a 2m staleness allowance because it sleeps",
            "- lapbox: node_exporter only (see below)",
            "",
            "Exporters are installed by the ansible `monitoring_agent` role; versions are pinned in "
            "`group_vars/all/monitoring.yml`."]),
        ("Grafana", ["Anonymous access off, one admin plus a read-only viewer account for the "
                     "wall tablet. Data sources: Prometheus (default), Loki, and the postgres "
                     "`db` read-only user for paperless statistics. Plugins are baked into the "
                     "image, never installed at runtime."]),
        ("lapbox exporter", ["node_exporter on lapbox listens only on its tailscale address "
                              "`100.64.0.30:9100`, not the LAN IP, because the laptop roams onto "
                              "untrusted networks. Prometheus scrapes it through the tunnel; a "
                              "LAN-IP target will always be down."]),
        ("Alertmanager", ["Routes everything to the ntfy topic `homelab-alerts` on "
                          "`ntfy.home.example.com`. Critical alerts repeat every 1h, warnings 12h. "
                          "Inhibit rule: HostDown suppresses that host's other alerts."]),
        ("Loki", ["Promtail on each host ships journald and docker logs. Retention 14 days "
                  "(`retention_period: 336h`), compactor enabled."]),
        ("Alert rules", ["- DiskAlmostFull: >85% for 15m", "- ZpoolDegraded: immediate",
                         "- BackupTooOld: no restic snapshot in 30h",
                         "- CertExpiringSoon: < 14 days", "- GpuHot: > 83 C for 10m"]),
        ("Dashboards", ["Provisioned JSON in the ansible repo under `roles/grafana/files`. Export "
                        "from the UI and commit, otherwise changes vanish on redeploy."]),
        ("Silences", ["Planned maintenance: `amtool silence add instance=~\"vmhost.*\" "
                      "--duration=2h`. Remove silences when done instead of letting them expire."]),
        _changelog(c, ["added the zfs exporter", "moved alerting from e-mail to ntfy",
                       "raised prometheus retention", "added blackbox probes for every name",
                       "restricted the lapbox exporter to tailscale", "added GpuHot",
                       "deleted the unused snmp job", "loki compactor enabled"]),
    ]
    body = ("Reference for the monitoring stack. Long-lived; update when jobs or retention change.\n\n"
            + "\n".join(_section(n, ls) for n, ls in sections))
    slug = c.add(Doc(slug="monitoring-stack-reference", title="Monitoring stack reference",
                     body=body, host="apphost", importance=4,
                     tags=["monitoring", "prometheus", "alerting", "loki"], volatility="durable"))
    A = c.ask
    A("current-state", "what's the current prometheus retention period", slug, why="retention section")
    A("paraphrase", "where do alert notifications get sent", slug, why="alertmanager route")
    A("real", "why can't prometheus scrape the lapbox node exporter on its lan ip", slug,
      why="tailscale-only exporter")
    A("identifier", "homelab-alerts ntfy topic", slug, ("uptime-kuma-status", 1), why="alert route")
    c.monitoring = slug  # type: ignore[attr-defined]


def network_overview(c: Corpus) -> None:
    sections = [
        ("Summary", ["One router, one 8-port managed switch, two access points. Everything that "
                     "matters is on VLAN 1; the other VLANs are isolated by the router's firewall."]),
        ("VLAN 1 (lab)", ["10.10.1.0/24, gateway .1. Static: apphost .10, vmhost .11, gpuhost "
                          ".20, lapbox .30 (DHCP reservation), printer .40. DHCP pool .100-.199."]),
        ("VLAN 110 (guests)", ["10.10.110.0/24, internet only, client isolation on the APs. SSID "
                              "`hl-guest`, password rotated yearly."]),
        ("VLAN 120 (IoT)", ["10.10.120.0/24. Smart plugs, sensors and the TV. No route to "
                           "10.10.1.0/24 except TCP 1883 to apphost (MQTT) and UDP 123 for "
                           "time. Internet blocked for everything except the TV."]),
        ("VLAN 130 (cameras)", ["10.10.130.0/24, no internet at all. The NVR container pulls RTSP "
                               "streams from here; cameras cannot initiate connections."]),
        ("Switch", ["Port 1 uplink to router (trunk), 2 apphost, 3 vmhost (trunk for guests), 4 "
                    "gpuhost, 5-6 APs (trunk), 7 printer, 8 spare. Config backup in the ansible "
                    "repo."]),
        ("Wi-Fi", ["SSIDs `hl` (VLAN 1), `hl-iot` (VLAN 120, 2.4 GHz only), `hl-guest` (VLAN "
                   "110). Roaming works between the two APs with 802.11r off."]),
        ("Router", ["Small x86 router appliance running an open-source firewall distribution. "
                    "WAN via the ISP's modem in bridge mode, IPv6 prefix delegation /56 with one "
                    "/64 per VLAN, though only VLAN 1 and 20 get router advertisements. Config "
                    "backups are exported monthly into the ansible repo."]),
        ("Firewall rules", ["- VLAN 1 -> any: allow",
                            "- VLAN 110 -> RFC1918: block, -> internet: allow",
                            "- VLAN 120 -> 10.10.1.10:1883/tcp, :123/udp: allow; rest of RFC1918: block",
                            "- VLAN 130 -> any: block (the NVR connects in, not out)",
                            "- WAN -> any: block except the tailscale UDP port, which is not forwarded "
                            "because NAT traversal works without it"]),
        ("DNS and DHCP", ["The router does DHCP for every VLAN and hands out 10.10.1.10 "
                          "(AdGuard) as resolver on VLAN 1 and 30; guests get public resolvers."]),
        _changelog(c, ["split IoT devices into VLAN 120", "added the camera VLAN", "second access "
                       "point in the garden room", "replaced the unmanaged switch",
                       "moved DHCP from apphost to the router", "802.11r turned off after "
                       "roaming problems", "guest password rotated", "IPv6 on the lab VLAN"]),
    ]
    body = ("How the home network is laid out.\n\n"
            + "\n".join(_section(n, ls) for n, ls in sections))
    slug = c.add(Doc(slug="home-network-overview", title="Home network overview", body=body,
                     importance=4, tags=["network", "vlan", "wifi"], volatility="durable"))
    A = c.ask
    A("paraphrase", "can the smart plugs talk to the main network", slug, ("mosquitto-auth", 1),
      why="IoT VLAN section")
    A("identifier", "VLAN 130 cameras", slug, why="camera VLAN section")
    A("real", "whats the dhcp range on the lab vlan", slug, why="VLAN 1 section")


def gpuhost_setup(c: Corpus) -> None:
    sections = [
        ("Hardware", ["Desktop tower, 16-core CPU, 96 GB RAM, one NVIDIA GPU with 24 GB, onboard "
                      "2.5GbE (eno1) plus a 10GbE PCIe card (enp5s0) to the switch's SFP+ port.",
                      "",
                      "- PSU: 1000 W, 80+ Gold, fully modular",
                      "- CPU cooler: 360 mm AIO, pump on the CPU_PUMP header at full speed",
                      "- Case: mid tower, three front intakes, one rear and two top exhausts",
                      "- Spare slots: one x4 PCIe slot free, two DIMM slots free"]),
        ("Operating system", ["Debian testing with backports kernels, installed from the netinst "
                              "image. Unattended upgrades are off: the NVIDIA DKMS module has to "
                              "rebuild against every kernel, so updates are done by hand with the "
                              "driver check afterwards. Packages beyond the base: build-essential, "
                              "cmake, ccache, ffmpeg, nvtop, btop, podman, git-lfs."]),
        ("Networking", ["Static 10.10.1.20 on enp5s0 via systemd-networkd; eno1 stays up "
                        "without an address only for wake-on-lan. MTU 9000 on the 10GbE link to "
                        "match apphost for NFS model copies. Hostname resolution via AdGuard."]),
        ("Users", ["One login user plus a system user `llm` that owns /srv/models and runs "
                   "the model services. The login user is in the `render` and `video` groups."]),
        ("Firmware settings", ["Resizable BAR on, CSM off, Secure Boot off (NVIDIA DKMS), XMP "
                               "profile 1, Power On By PCI-E enabled for wake-on-lan."]),
        ("Disks", ["2 TB NVMe for the OS (ext4), 4 TB NVMe for models and datasets mounted at "
                   "/srv/models. No RAID; models are re-downloadable."]),
        ("Kernel parameters", ["`pcie_aspm=off` on the kernel command line: with ASPM enabled "
                               "the 10GbE card drops its link every few hours (`enp5s0: Link is "
                               "Down`). `nvidia-drm.modeset=1` for Wayland."]),
        ("GPU power limit", ["A oneshot unit `gpu-power-limit.service` runs `nvidia-smi -pl 280` "
                             "at boot: 280 W instead of the default 350 W costs ~4% speed and "
                             "makes the card much quieter."]),
        ("Services", ["llama-server (user unit), immich machine-learning container, syncthing, "
                      "node_exporter and nvidia_gpu_exporter."]),
        ("Sleep", ["Suspends after 30 minutes idle unless llama-server has an active request "
                   "(an inhibitor script checks its /slots endpoint)."]),
        ("Desktop", ["KDE Plasma on Wayland, two monitors on DisplayPort."]),
        _changelog(c, ["installed the 10GbE card", "moved models to the second NVMe",
                       "switched to the open kernel modules", "added the sleep inhibitor",
                       "enabled resizable BAR", "replaced the stock cooler with the AIO",
                       "added the GPU power limit unit", "turned off ASPM"]),
    ]
    body = ("Setup notes for gpuhost, the GPU workstation.\n\n"
            + "\n".join(_section(n, ls) for n, ls in sections))
    slug = c.add(Doc(slug="gpuhost-setup-notes", title="gpuhost setup notes", body=body,
                     host="gpuhost", importance=4, tags=["gpuhost", "hardware", "setup"],
                     volatility="durable"))
    A = c.ask
    A("identifier", "pcie_aspm=off enp5s0", slug, why="kernel parameters section")
    A("paraphrase", "why does the 10 gig network card on the workstation keep losing its link", slug,
      why="ASPM")
    A("current-state", "what's the gpu power limit on gpuhost currently", slug, why="power limit")


def lapbox_workstation(c: Corpus) -> None:
    sections = [
        ("Shell", ["zsh with a small hand-written prompt, no framework. History shared across "
                   "sessions, 50k lines."]),
        ("Packages", ["Base plus: base-devel, paru for the AUR, podman, uv, neovim, kitty, "
                      "tmux, ripgrep, fd, bat, jq, sbctl, tlp, syncthing, restic (for the home "
                      "directory backup to apphost), wireguard-tools, tailscale."]),
        ("Power", ["TLP with the battery charge limit at 80%; `powertop --auto-tune` is not used "
                   "because it breaks the USB dock's ethernet. Lid close suspends, hibernate is "
                   "not configured (zram swap only)."]),
        ("Backups", ["The home directory goes to the restic repository on apphost's NAS share "
                     "every evening via a user timer when on the LAN; excludes ~/.cache, "
                     "~/Downloads and every .venv."]),
        ("tmux", ["Prefix is `C-a` (not the default C-b). Splits on `|` and `-`, mouse on, "
                  "`tmux-resurrect` restores sessions after reboot."]),
        ("Terminal", ["kitty. The `ssh` kitten breaks on vmhost's guests because they lack the "
                      "xterm-kitty terminfo; there, use plain ssh with `TERM=xterm-256color` "
                      "(an alias `sshx` does this)."]),
        ("Editor", ["neovim with lazy.nvim; LSPs via mason: pyright, ruff, gopls, yamlls."]),
        ("Git", ["Commits signed with the ssh key (`gpg.format ssh`), `pull.rebase true`, "
                 "`rerere.enabled true`."]),
        ("Fonts", ["A monospace nerd font at 11pt; UI font system default."]),
        ("Browser", ["Firefox with a separate profile for the lab admin UIs."]),
        ("Dotfiles", ["Managed with a bare git repo in ~/.dotfiles, pushed to the forge."]),
        _changelog(c, ["moved from bash to zsh", "tmux prefix changed to C-a", "kitty replaced "
                       "alacritty", "neovim config rewritten for lazy.nvim", "commit signing with "
                       "ssh keys", "added the sshx alias", "charge limit set to 80%",
                       "dotfiles moved to a bare repo"]),
    ]
    body = ("How lapbox (the Arch laptop) is set up for daily work.\n\n"
            + "\n".join(_section(n, ls) for n, ls in sections))
    slug = c.add(Doc(slug="lapbox-workstation-config", title="lapbox workstation config", body=body,
                     host="lapbox", importance=3, tags=["lapbox", "dotfiles", "terminal"],
                     volatility="durable"))
    A = c.ask
    A("real", "tmux on lapbox whats the prefix again", slug, why="tmux section")
    A("paraphrase", "the terminal is garbled when I ssh from the laptop into the test VMs", slug,
      ("ssh-proxyjump-config", 1), why="kitty terminfo")


def dr_plan(c: Corpus) -> None:
    sections = [
        ("Scope", ["Losing apphost (disk failure, fire, theft) is the scenario; the other hosts "
                   "are rebuildable from ansible and hold nothing unique."]),
        ("What is backed up", ["restic nightly: /srv/data, /srv/compose, /srv/backup/pg, /etc. "
                               "Not backed up: /srv/media (re-rippable), prometheus TSDB."]),
        ("Where", ["Primary repo on the NAS share `/mnt/backup/restic`; weekly offsite copy by "
                   "rclone. See the offsite note for the bucket."]),
        ("Hardware spares", ["A cold-spare 8 TB disk for tank (once the replacement arrives), a "
                             "spare 1 TB SSD for the OS, and the previous mini PC in the cupboard "
                             "that can run the core stacks temporarily with 16 GB of RAM."]),
        ("Accounts needed", ["The DNS provider account (for the wildcard certificate), the "
                             "object storage account (offsite copy), the tailscale admin console, "
                             "and the domain registrar. Recovery codes for all four are in the "
                             "password manager and printed in the fire safe."]),
        ("Rebuild time", ["Measured estimate: 2 hours for OS, pool and base stacks from ansible; "
                          "restores are limited by the NAS read speed (about 180 MB/s), so a full "
                          "restore of /srv/data is roughly 1.5 hours more."]),
        ("Secrets", ["The restic repository password is in `/root/.config/restic/pw` on "
                     "apphost (mode 400) and on paper in the fire safe. Without it the backups "
                     "are useless; the ansible vault has a third copy."]),
        ("Restore order", ["1. Base OS and ZFS pool via ansible. 2. `db` and restore the latest "
                           "pg dump. 3. AdGuard, so names resolve. 4. caddy. 5. Everything else, "
                           "most important first: vaultwarden, paperless, home-assistant."]),
        ("Restore test", ["Quarterly: restore one stack into vmhost's sandbox VM and click "
                          "through it. Record the date in this note."]),
        ("Last test", ["Restored paperless into VM 120 on 2026-07-12: 38 minutes end to end, "
                       "documents and tags intact."]),
        _changelog(c, ["plan written after the borg USB disk died", "offsite copy added",
                       "restore order revised: DNS before caddy", "first quarterly restore test",
                       "paper copy of the repository password added", "mini PC kept as a spare"]),
    ]
    body = ("Disaster recovery plan for the lab.\n\n"
            + "\n".join(_section(n, ls) for n, ls in sections))
    slug = c.add(Doc(slug="disaster-recovery-plan", title="Disaster recovery plan", body=body,
                     importance=5, tags=["backup", "disaster-recovery", "restic"], volatility="durable"))
    A = c.ask
    A("agent-framed", "I'm writing a restore script for apphost. Where is the restic repository "
      "password kept?", slug, why="secrets section")
    A("paraphrase", "in what order do we bring services back after losing the server", slug,
      why="restore order")
    A("current-state", "when was the last restore test and how long did it take", slug, why="last test")
    c.dr = slug  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Superseded notes: never indexed; the replacement answers.
# ---------------------------------------------------------------------------

def superseded(c: Corpus) -> None:
    olds = [
        ("wireguard-vpn-apphost", "WireGuard VPN on apphost", "tailscale-subnet-router",
         "WireGuard on apphost, UDP 51820 forwarded on the router, peers in /etc/wireguard/wg0.conf. "
         "Replaced by the tailscale subnet router."),
        ("pihole-on-vmhost", "Pi-hole on vmhost", None,
         "Pi-hole ran in an LXC on vmhost as the LAN resolver. Replaced by AdGuard Home on apphost."),
        ("apphost-root-crontab", "apphost root crontab jobs", "apphost-systemd-timers",
         "restic at 03:00, pg_dumpall at 02:30 and docker prune weekly, all in root's crontab."),
        ("docker-json-file-logs", "Docker logging defaults", "docker-log-rotation",
         "Containers use the default json-file log driver without limits."),
        ("borg-usb-backups", "Backups with borg to a USB disk", "disaster-recovery-plan",
         "borg create to a USB disk plugged into apphost, rotated monthly to a drawer."),
    ]
    for slug, title, by, body in olds:
        c.add(Doc(slug=slug, title=title, body=body + "\n\nSuperseded; kept for history.",
                  importance=2, tags=["superseded"],
                  superseded_by=by or c.dns_current))  # type: ignore[attr-defined]
    c.ask("paraphrase", "wireguard vpn config for getting into the lab remotely",
          "tailscale-subnet-router", why="wireguard was superseded by tailscale")
    c.ask("real", "is the crontab on apphost where the backup job is scheduled?",
          "apphost-systemd-timers", why="crontab superseded by timers")


# ---------------------------------------------------------------------------
# Unqueried distractors with overlapping vocabulary.
# ---------------------------------------------------------------------------

def distractors(c: Corpus) -> None:
    misc = [
        ("rack-shopping-list", "Rack shopping list", ["hardware", "todo"],
         "Wanted: a 12U wall rack, a second 8 TB disk as a cold spare for tank, a USB-C dock for "
         "lapbox, short DAC cables for the 10GbE link, and a bigger UPS battery."),
        ("ideas-move-grafana-to-vmhost", "Idea: move monitoring to vmhost", ["ideas", "monitoring"],
         "Maybe run grafana and prometheus in a vmhost VM so a apphost outage does not also "
         "blind the monitoring. Needs its own backups and a second alert path. Not decided."),
        ("reading-list-selfhosting", "Reading list: self-hosting", ["reading"],
         "Articles to read about ZFS special vdevs, restic vs borg performance, DNS-over-QUIC "
         "support in resolvers, and running llama.cpp with speculative decoding."),
        ("lab-power-consumption", "Lab power consumption", ["power", "cost"],
         "Measured at the UPS: apphost 58 W idle, vmhost 41 W, gpuhost 9 W asleep and 420 W "
         "under full GPU load, switch and APs 22 W."),
        ("printer-toner-log", "Printer toner log", ["printer"],
         "Toner replaced 2026-02-11 and 2026-08-30. Drum at 61% in August."),
        ("tv-vlan-exception", "TV internet exception on the IoT VLAN", ["network", "iot"],
         "The TV needs streaming apps, so it has a firewall exception for internet access on "
         "VLAN 120. It still cannot reach the lab VLAN."),
        ("vmhost-gpu-passthrough-attempt", "Tried GPU passthrough on vmhost", ["proxmox", "gpu"],
         "Tried passing an old GPU into VM 120 for CUDA tests. IOMMU groups were not clean on this "
         "board; gave up and use gpuhost instead."),
        ("ansible-vault-howto", "Ansible vault usage", ["ansible", "secrets"],
         "Secrets in `group_vars/all/vault.yml`, encrypted with `ansible-vault`. The vault "
         "password file is ~/.config/ansible/vault-pass on lapbox, never committed."),
        ("kids-tablet-dns-filter", "Tablet DNS filtering", ["dns", "family"],
         "The tablets use an AdGuard client rule with the family blocklist and safe search "
         "enforced. Identified by their DHCP reservation, not MAC."),
        ("homeassistant-automations-backup", "Home Assistant automations", ["home-assistant"],
         "Automations are written in YAML in the config volume and committed to the forge weekly "
         "by a small script; the UI editor is used only for testing."),
        ("forge-mirror-settings", "Forge push mirrors", ["forge", "git"],
         "Public projects push-mirror to an external host every 8 hours. Private repos are never "
         "mirrored."),
        ("ollama-trial", "Tried ollama on gpuhost", ["llm", "gpuhost"],
         "Tried ollama for a week; went back to llama-server because of finer control over "
         "context size and batch settings."),
        ("paperless-scanner-settings", "Scanner settings for paperless", ["paperless", "scanner"],
         "The scanner saves 300 dpi greyscale PDFs to the SMB share; duplex on; blank-page removal "
         "left to paperless."),
        ("lapbox-battery-health", "lapbox battery health", ["lapbox", "battery"],
         "Battery at 87% of design capacity after two years; charge limit 80% via TLP "
         "`STOP_CHARGE_THRESH_BAT0=80`."),
        ("jellyfin-subtitles", "Jellyfin subtitle downloads", ["jellyfin", "media"],
         "Subtitles are fetched by a plugin at scan time; forced subtitles only for foreign-language "
         "parts."),
        ("vaultwarden-backup-export", "Vaultwarden encrypted export", ["vaultwarden", "backup"],
         "Besides restic, an encrypted JSON export is made by hand every quarter and stored in "
         "the fire safe on a USB stick."),
        ("vmhost-ksm-sharing", "KSM on vmhost", ["proxmox", "memory"],
         "Kernel same-page merging saves about 3 GB across the Linux guests; left at the defaults."),
        ("immich-storage-template", "Immich storage template", ["immich", "photos"],
         "Library layout `{{y}}/{{MM}}/{{filename}}`; changing it later moves every file, so it "
         "was set once at the start."),
    ]
    for slug, title, tags, body in misc:
        c.add(Doc(slug=slug, title=title, body=body, importance=c.rng.choice([1, 1, 2, 2, 3]),
                  tags=tags))


# ---------------------------------------------------------------------------
# Multi-note questions (both notes needed).
# ---------------------------------------------------------------------------

def multi(c: Corpus) -> None:
    A = c.ask
    A("multi", "how are apphost backups made and how do they get copied offsite",
      c.restic_current, "rclone-offsite-copy", ("disaster-recovery-plan", 1),  # type: ignore[attr-defined]
      why="restic + rclone")
    A("multi", "postgres dumps and the nightly restic backup: what runs when",
      "postgres-shared-dumps", "apphost-systemd-timers", why="dump timer + timers list")
    A("multi", "power cut: what shuts down in which order and how do I wake gpuhost afterwards",
      "ups-nut-shutdown", "gpuhost-wake-on-lan", why="NUT + WoL")
    A("multi", "ssh key rules and the jump host config for the proxmox guests",
      "ssh-key-policy", "ssh-proxyjump-config", why="policy + ProxyJump")
    A("multi", "zfs scrub schedule and the failing sdc disk in tank",
      "zfs-scrub-schedule", "apphost-sdc-reallocated-sectors", why="scrub + smart")
    A("multi", "git server ssh port and where its CI runner is registered",
      "forge-ssh-port", "forge-actions-runner", why="forge + runner")
    A("multi", "gpuhost suspend fix with the nvidia driver and which driver it runs today",
      "gpuhost-suspend-resume-nvidia", c.gpu_current, why="suspend + current driver")  # type: ignore[attr-defined]
    A("multi", "zigbee coordinator path and mqtt broker login for zigbee2mqtt",
      "zigbee-coordinator-path", "mosquitto-auth", why="zigbee + mqtt")
    A("multi", "vmhost VMs and which proxmox version the host runs",
      "vmhost-vm-inventory", c.pve_current, why="inventory + current pve")  # type: ignore[attr-defined]
    A("multi", "why lapbox is on linux-lts and how secure boot signing works there",
      "lapbox-linux-lts-iwlwifi", "lapbox-secure-boot-sbctl", why="kernel pin + sbctl")
    A("multi", "immich: which version is deployed and where does its machine learning run",
      c.deploy_latest["immich"], "immich-ml-on-gpuhost", why="deploy + ML host")  # type: ignore[attr-defined]
    A("multi", "docker firewall rules and log rotation settings on apphost",
      "docker-firewall-docker-user", "docker-log-rotation", why="two docker configs")
    A("agent-framed", "I'm about to replace the failing disk in the storage pool on apphost. What "
      "do I need to know about the pool and the backups before I start?",
      "apphost-sdc-reallocated-sectors", ("zfs-scrub-schedule", 1), ("disaster-recovery-plan", 1),
      why="replacement procedure; context")
    A("agent-framed", "Going to reboot apphost for a kernel update. What else breaks or needs a "
      "manual fix afterwards?", "vmhost-nfs-stale-file-handle", ("docker-log-rotation", 1),
      why="NFS stale handle after apphost reboot")
    A("agent-framed", "Please rotate the DNS provider token that caddy uses for certificates. Where "
      "is it configured?", "caddy-dns-provider-module", why="token location")
    A("agent-framed", "I'm tuning alert rules. Which alerts exist today and where do they go?",
      c.monitoring, why="alert rules + route")  # type: ignore[attr-defined]
    A("agent-framed", "Set up a new DNS rewrite for a service. Which resolver are we running and "
      "which upstreams does it use at the moment?", c.dns_current, why="current resolver config")  # type: ignore[attr-defined]
    A("real", "ok so the backup failed?? check what restic said most recently", c.restic_current,
      why="latest check")  # type: ignore[attr-defined]
    A("real", "is the offsite thing actually immutable or can a bad sync wipe it",
      "rclone-offsite-copy", why="object lock")
    A("real", "llama-server model on gpuhost right now + how fast is it", c.llama_current,  # type: ignore[attr-defined]
      why="newest llama-server note")


def build(seed: int = SEED) -> Corpus:
    c = Corpus(seed)
    for step in (gpu_driver, restic_backups, dns_upstreams, proxmox_updates, lapbox_disk,
                 caddy_certs, llama_model, lapbox_updates, tank_usage, speedtest, deploys, facts, apphost_runbook,
                 monitoring_reference, network_overview, gpuhost_setup, lapbox_workstation,
                 dr_plan, superseded, distractors, multi):
        step(c)
    live = {s for s, d in c.docs.items() if not d.superseded_by}
    for q in c.queries:
        for slug, _ in q.gold:
            if slug not in live:
                raise ValueError(f"gold slug {slug!r} is missing or superseded ({q.query!r})")
    return c


def render_note(doc: Doc) -> str:
    note = Note(title=doc.title, slug=doc.slug, path=f"{doc.slug}.md", body=doc.body,
                profile=PROFILE, host=doc.host, importance=doc.importance,
                superseded_by=doc.superseded_by, tags=list(doc.tags), grounding="ok",
                description=doc.description, observed_at=doc.observed_at,
                volatility=doc.volatility)
    return dump_note(note)


def golden_rows(c: Corpus) -> list[dict]:
    """Rows in a seeded shuffled order; every third id is `split: test` (eval/README.md)."""
    order = list(range(len(c.queries)))
    random.Random(c.rng.random()).shuffle(order)
    rows = []
    for n, i in enumerate(order, 1):
        q = c.queries[i]
        rows.append({"id": f"p{n:03d}", "query": q.query, "category": q.category,
                     "split": "test" if n % 3 == 0 else "dev",
                     "gold": [{"slug": s, "grade": g} for s, g in q.gold], "why": q.why})
    return rows


def write(out: Path, seed: int = SEED) -> tuple[int, int]:
    """Write corpus/*.md and golden.jsonl under out; returns (notes, queries)."""
    c = build(seed)
    corpus = out / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for old in corpus.glob("*.md"):
        old.unlink()
    for slug in sorted(c.docs):
        (corpus / f"{slug}.md").write_text(render_note(c.docs[slug]), encoding="utf-8")
    rows = golden_rows(c)
    with (out / "golden.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(c.docs), len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent,
                        help="directory to write corpus/ and golden.jsonl into (default: eval/public)")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    notes, queries = write(args.out, args.seed)
    print(f"wrote {notes} notes and {queries} queries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
