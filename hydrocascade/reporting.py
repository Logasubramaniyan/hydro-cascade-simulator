"""Reproducible plots, hashes and run manifests."""

from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
import sys
import numpy as np
import pandas as pd
import scipy
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from . import __version__
from .data import file_hash

PALETTE = ["#007f8b", "#f39c42", "#5864b0", "#bc5475"]


def plot_dispatch(baseline, optimized, output, dt_seconds=3600):
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "svg.hashsalt": "hydro-cascade-v0.2",
            "svg.fonttype": "none",
        }
    )
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True, layout="constrained")
    fig.patch.set_facecolor("#f5f8fa")
    total = baseline.groupby("step").power_mw.sum()
    chosen = optimized.groupby("step").power_mw.sum()
    hours = total.index.to_numpy() * dt_seconds / 3600
    axes[0].plot(hours, total, label="Inflow-following rule", color="#a2adb8", linewidth=2)
    axes[0].plot(hours, chosen, label="Optimized schedule", color=PALETTE[0], linewidth=2)
    axes[0].set_ylabel("Generation (MW)")
    axes[0].legend(loc="upper left", frameon=False)
    second = axes[0].twinx()
    second.plot(
        hours,
        optimized.groupby("step").price_eur_mwh.first(),
        color=PALETTE[1],
        alpha=0.65,
        linewidth=1.5,
        linestyle="--",
    )
    second.set_ylabel("Input price (EUR/MWh)", color=PALETTE[1])
    for color, (plant, frame) in zip(PALETTE, optimized.groupby("plant_id", sort=False)):
        axes[1].plot(
            frame.step * dt_seconds / 3600, frame.storage_m3 / 1e6, label=plant, color=color, linewidth=2
        )
    axes[1].set_ylabel("Storage (million m³)")
    axes[1].legend(frameon=False, ncol=3)
    residual = optimized.groupby("step").water_residual_m3.apply(lambda x: x.abs().max())
    transit = optimized.groupby("step").transit_m3.first() / 1e6
    axes[2].fill_between(hours, transit, color=PALETTE[2], alpha=0.2)
    axes[2].plot(hours, transit, color=PALETTE[2], linewidth=2)
    axes[2].set_ylabel("Water in transit (million m³)")
    axes[2].text(
        0.02,
        0.93,
        f"Max plant water-balance residual: {residual.max():.2e} m³",
        transform=axes[2].transAxes,
        va="top",
        fontsize=10,
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85},
    )
    axes[2].set_xlabel("Elapsed hours")
    axes[2].set_ylim(0, max(0.1, transit.max() * 1.3))
    for ax in axes:
        ax.grid(axis="y", alpha=0.18)
        ax.set_facecolor("white")
    fig.suptitle(
        "Hydro Cascade Lab  |  scheduling, storage and conservation\nSynthetic fixed-head cascade • known-price benchmark",
        fontsize=15,
        fontweight="bold",
    )
    fig.savefig(output, metadata={"Date": None}, dpi=150)
    plt.close(fig)


def plot_policy_comparison(results, output):
    names = ["inflow_following", "rolling_deterministic", "rolling_stochastic", "perfect_foresight"]
    fig, ax = plt.subplots(figsize=(11, 5), layout="constrained")
    fig.patch.set_facecolor("#f5f8fa")
    for i, name in enumerate(names):
        values = results[results.policy == name].objective_eur / 1000
        ax.scatter(
            np.full(len(values), i) + np.linspace(-0.12, 0.12, len(values)),
            values,
            color=PALETTE[i],
            alpha=0.55,
            s=25,
        )
        ax.scatter(i, values.mean(), color=PALETTE[i], marker="D", s=100)
    ax.set_xticks(
        range(4),
        ["Inflow rule", "Rolling\ndeterministic", "Rolling\nstochastic", "Perfect foresight\nbenchmark"],
    )
    ax.set_ylabel("Revenue + terminal value − shortfall penalty (thousand EUR)")
    ax.set_title("Held-out synthetic paths | dots = trials, diamonds = means", fontsize=14, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.18)
    fig.savefig(output, metadata={"Date": None}, dpi=150)
    plt.close(fig)


def write_manifest(output, assumptions, source_paths=(), audits=None):
    directory = Path(output)
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        # A source directory embedded in another repository is not its own Git revision.
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if Path(root).resolve() != Path(__file__).parent.parent.resolve():
            revision = None
    except (OSError, subprocess.CalledProcessError):
        revision = None
    sources = {str(Path(p).name): file_hash(p) for p in source_paths}
    code = {
        str(p.relative_to(Path(__file__).parent.parent)): file_hash(p)
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }
    artifact_hashes = {
        str(p.relative_to(directory)): file_hash(p)
        for p in directory.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    manifest = dict(
        created_utc=datetime.now(timezone.utc).isoformat(),
        version=__version__,
        git_revision=revision,
        python=sys.version,
        platform=platform.platform(),
        dependencies=dict(
            numpy=np.__version__,
            pandas=pd.__version__,
            scipy=scipy.__version__,
            matplotlib=matplotlib.__version__,
        ),
        assumptions=assumptions,
        source_sha256=sources,
        code_sha256=code,
        artifact_sha256=artifact_hashes,
        solver_audits=audits or {},
    )
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8"
    )
    return manifest
