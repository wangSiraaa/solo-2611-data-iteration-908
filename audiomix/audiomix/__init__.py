"""audiomix：多采样率剪辑时间轴与真实混音档案 + 地方台外送节目制作线。"""
from .assets import AssetRegistry
from .timeline import TimelineEditor, Clip, ValidationError
from .engine import (
    render_reference, render_block, render_full_vectorized,
    validate_product, output_sample_count, BLOCK_SAMPLES, OUTPUT_RATE,
)
from .archive import ArchiveStore, block_key_of
from .export_licenses import LicenseLedger, License, WILDCARD_REGION
from .export_usage import (
    SourceUse, source_uses_at, iter_clip_uses, unlicensed_report,
    unlicensed_output_points, clip_is_fully_licensed,
)
from .export_schedule import (
    Sentence, BackupRecording, Shift, Arrangement, SchedulePlanner,
)
from .export_counter import DispatchOffice, IssuedTicket
from .export_program import ExportStudio

__all__ = [
    "AssetRegistry", "TimelineEditor", "Clip", "ValidationError",
    "render_reference", "render_block", "render_full_vectorized",
    "validate_product", "output_sample_count", "BLOCK_SAMPLES", "OUTPUT_RATE",
    "ArchiveStore", "block_key_of",
    "LicenseLedger", "License", "WILDCARD_REGION",
    "SourceUse", "source_uses_at", "iter_clip_uses", "unlicensed_report",
    "unlicensed_output_points", "clip_is_fully_licensed",
    "Sentence", "BackupRecording", "Shift", "Arrangement", "SchedulePlanner",
    "DispatchOffice", "IssuedTicket", "ExportStudio",
]
