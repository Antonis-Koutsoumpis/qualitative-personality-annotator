"""Create small synthetic sample items (2 videos, 2 audio files, 2 texts) for testing.

    python tools/make_sample_data.py [output_folder]
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from personality_annotator.extract import ffmpeg_exe  # noqa: E402

TEXTS = {
    "interview_01.txt": (
        "Honestly, I love meeting new people. Last year I organised a neighbourhood "
        "festival on my own, and I spoke to everyone who walked past our stand.\n\n"
        "I do get nervous before big decisions, though. I usually call my sister and "
        "talk it through for an hour before I commit to anything.\n\n"
        "At work I keep a detailed planner. Every Sunday evening I plan the week, "
        "and I rarely miss a deadline."
    ),
    "interview_02.txt": (
        "I prefer quiet evenings with a book. Parties tire me out quickly, and I tend "
        "to leave early.\n\n"
        "If someone wrongs me, I let it go fairly quickly; holding grudges just costs "
        "energy.\n\n"
        "I am fascinated by how languages change over time, and I spend weekends "
        "reading about etymology and visiting museums."
    ),
}


def run(cmd):
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *cmd], check=True)


def main(out: Path):
    for sub in ("videos", "audio", "texts"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for i, (src, freq) in enumerate([("testsrc2", 440), ("smptebars", 660)], 1):
        run(["-f", "lavfi", "-i", f"{src}=size=480x270:rate=25:duration=30",
             "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=30",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "64k", "-shortest", "-movflags", "+faststart",
             str(out / "videos" / f"sample_video_{i:02d}.mp4")])
        run(["-f", "lavfi", "-i",
             f"sine=frequency={freq}:duration=25,volume='0.3+0.25*sin(2*PI*t/4)':eval=frame",
             "-c:a", "libmp3lame", "-b:a", "64k", str(out / "audio" / f"sample_audio_{i:02d}.mp3")])
    for name, text in TEXTS.items():
        (out / "texts" / name).write_text(text, encoding="utf-8")
    print(f"Sample data written to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "sample_data")
