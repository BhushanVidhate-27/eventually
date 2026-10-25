# 60–90 second demo

No live server. No cloud account. The demo runs on the laptop, against a local Ollama model.

## Before recording

```bash
cd ~/Life/hf26-week3

# Make sure the local model server is up.
ollama serve

# Pull the small model once (1.3 GB).
ollama pull llama3.2:1b

# Build the demo folder without recording the setup.
rm -rf demo-downloads
python3 eventually.py --demo > /dev/null
```

Open a clean terminal in this folder. Use a dark theme, 16–18 pt type, and a window about 100 columns wide. The plan output is 60 lines; a 90-second video can scroll once.

## The three beats

### 0:00–0:30 — Plan

```bash
ls demo-downloads | head -12
python3 eventually.py demo-downloads
```

Say:

> This is my Downloads folder, or a stand-in with the same problem: twenty files, no system. Eventually reads only the names. It asks a local Llama model where each one belongs, and prints a plan. Nothing has moved yet. Twenty files into eleven folders in under ten seconds, and zero bytes uploaded.

Point at the summary line:

```text
20 files (827 B) → 11 folders · 9.7s · 0 bytes uploaded · 0 files moved.
```

### 0:30–0:60 — Apply

```bash
python3 eventually.py demo-downloads --apply --state-dir .eventually-demo
find demo-downloads -maxdepth 2 -type f | sort
```

Say:

> The second command applies the plan. It creates the folders, moves the files, refuses to overwrite anything, and writes a manifest so I can take it back. The tree is boring now. That is the point.

The apply output ends with:

```text
Moved 20 files into 11 folders in 0.0s.
Manifest: .eventually-demo/last-run.json
Undo: python3 eventually.py --undo
```

### 0:60–0:90 — Undo

```bash
python3 eventually.py --undo --state-dir .eventually-demo
ls demo-downloads | wc -l
```

Say:

> And undo puts all twenty back. The folder is messy again, on purpose. That's the whole tool: one clear job, a local open-weight model, and a way back.

The undo output:

```text
Restored 20 files in 0.0s. The folder is messy again, on purpose.
```

End card: **Eventually — your folder, eventually.** Repo link in the description.

## Recording tips

- Record the whole thing in one take; the tool is deterministic enough with `temperature: 0` and `seed: 42`.
- If the first run is slow because Ollama is loading the model, do a warm-up run before recording: `python3 eventually.py demo-downloads > /dev/null`.
- Do not cut the summary line. It is the proof: 20 files, 9.7 seconds, zero bytes uploaded, zero files moved.
- If you want the model's reasons on screen, add `--explain` to the first command. The default plan is cleaner.
- Upload to YouTube, Loom, or Vimeo and put the link in the DEV post's `## Demo` section. The repo also works as a self-contained demo without a video.
