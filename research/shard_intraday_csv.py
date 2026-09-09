"""Create deterministic physical intraday shards with causal warm-up overlap."""

from __future__ import annotations

import argparse, copy, csv, json
from pathlib import Path


def shard_csv(source: Path, output: Path, *, evaluation_sessions: int = 126,
              warmup_sessions: int = 220) -> Path:
    dates=[]; seen=set()
    with source.open("r",newline="",encoding="utf-8-sig") as handle:
        reader=csv.DictReader(handle)
        for row in reader:
            day=str(row["timestamp"])[:10]
            if day not in seen: seen.add(day); dates.append(day)
    dates.sort(); output.mkdir(parents=True,exist_ok=True); shards=[]
    for number,start_index in enumerate(range(0,len(dates),evaluation_sessions),start=1):
        end_index=min(len(dates),start_index+evaluation_sessions)-1
        warmup_index=max(0,start_index-warmup_sessions)
        filename=f"{source.stem}-shard-{number:03d}.csv"; destination=output/filename
        shards.append({"number":number,"filename":filename,"warmup_start":dates[warmup_index],"evaluation_start":dates[start_index],"evaluation_end":dates[end_index],"evaluation_sessions":end_index-start_index+1})
    handles={}; writers={}
    try:
        with source.open("r",newline="",encoding="utf-8-sig") as handle:
            reader=csv.DictReader(handle); fields=list(reader.fieldnames or ())
            for shard in shards:
                stream=(output/shard["filename"]).open("w",newline="",encoding="utf-8")
                handles[shard["number"]]=stream; writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader(); writers[shard["number"]]=writer
            for row in reader:
                day=str(row["timestamp"])[:10]
                for shard in shards:
                    if shard["warmup_start"]<=day<=shard["evaluation_end"]: writers[shard["number"]].writerow(row)
    finally:
        for handle in handles.values(): handle.close()
    manifest=output/"shards.json"
    manifest.write_text(json.dumps({"source":source.name,"evaluation_sessions_per_shard":evaluation_sessions,"warmup_sessions":warmup_sessions,"shards":shards},indent=2),encoding="utf-8")
    return manifest


def build_jobs(template_path: Path, shard_manifest_path: Path, output: Path,
               *, implementation_commit: str) -> list[Path]:
    template=json.loads(template_path.read_text(encoding="utf-8")); manifest=json.loads(shard_manifest_path.read_text(encoding="utf-8")); paths=[]
    declared_path=template_path.with_name("intraday-strategy-generation-001-shards.json")
    declared=json.loads(declared_path.read_text(encoding="utf-8")) if declared_path.is_file() else {}
    declared_by_file={row["filename"]:row for row in declared.get("shards",[])}
    for shard in manifest["shards"]:
        declaration=declared_by_file.get(shard["filename"])
        if not declaration: raise ValueError(f"shard is not predeclared: {shard['filename']}")
        job=copy.deepcopy(template); job.update({"job_id":declaration["job_id"],"bars_csv":shard["filename"],"implementation_commit":implementation_commit})
        job["experiment"]["trial_id"]=f"intraday-generation-001-baseline10-shard-{shard['number']:03d}"
        job["intraday_config"]["evaluation_start_date"]=shard["evaluation_start"]
        job["intraday_config"]["evaluation_end_date"]=shard["evaluation_end"]
        destination=output/f"{job['job_id']}.json"; destination.write_text(json.dumps(job,indent=2),encoding="utf-8"); paths.append(destination)
    return paths


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--source",type=Path,required=True); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--evaluation-sessions",type=int,default=126); parser.add_argument("--warmup-sessions",type=int,default=220); parser.add_argument("--job-template",type=Path); parser.add_argument("--implementation-commit"); args=parser.parse_args()
    manifest=shard_csv(args.source,args.output,evaluation_sessions=args.evaluation_sessions,warmup_sessions=args.warmup_sessions); print(manifest)
    if args.job_template:
        if not args.implementation_commit: parser.error("--implementation-commit is required with --job-template")
        for path in build_jobs(args.job_template,manifest,args.output,implementation_commit=args.implementation_commit): print(path)


if __name__=="__main__": main()
