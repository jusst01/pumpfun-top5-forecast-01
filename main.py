#!/usr/bin/env python3
import os
import sys
import json
import math
import random
from datetime import datetime, timezone

def parse_iso(s):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

def main():
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(output_dir, exist_ok=True)

    task_path = os.path.join(input_dir, "task.json")
    history_path = os.path.join(input_dir, "history.ndjson")

    if not os.path.exists(task_path) or not os.path.exists(history_path):
        # Fallback for local testing if paths differ
        input_dir = "../kit/input"
        task_path = os.path.join(input_dir, "task.json")
        history_path = os.path.join(input_dir, "history.ndjson")

    with open(task_path, "r") as f:
        task = json.load(f)

    history_until = parse_iso(task["history_until"])
    reads = [parse_iso(r) for r in task["reads"]]
    edges = task["size"]["edges"] # 81 edges

    marks = []
    with open(history_path, "r") as f:
        for line in f:
            if line.strip():
                marks.append(json.loads(line))

    if len(marks) <= 3:
        last_rows = marks[-1].get("rows", [])[:5] if marks else []
        last_mints = [r["mint"] for r in last_rows]
        probabilities = []
        for idx, mint in enumerate(last_mints):
            place = idx + 1
            probabilities.append({"mint": mint, "place": place, "p": 0.30})
            if place > 1:
                probabilities.append({"mint": last_mints[place - 2], "place": place, "p": 0.10})
            if place < 5:
                probabilities.append({"mint": last_mints[place], "place": place, "p": 0.10})
        top5 = last_mints + [""] * (5 - len(last_mints))
        sizes = []
        for idx, r in enumerate(last_rows):
            place = idx + 1
            cap_lamports = int(r["market_cap_lamports"])
            log_cap = math.log10(cap_lamports) - 9.0
            bin_idx = 81 if log_cap >= 6.0 else (0 if log_cap < 2.0 else next((i for i, e in enumerate(edges) if log_cap < e), 81))
            sizes.append({"place": place, "bin": bin_idx, "p": 0.70})
        forecast = {"probabilities": probabilities, "top5": top5, "sizes": sizes}
        output_path = os.path.join(output_dir, "forecast.json")
        with open(output_path, "w") as f:
            json.dump(forecast, f, indent=2)
        return

    # Parse coin histories
    coin_data = {} # mint -> { 'symbol': ..., 'created_at': ..., 'caps': [(time_sec, log_cap, venue)] }
    all_history_mints = set()

    for m in marks:
        at_time = parse_iso(m["at"])
        for r in m.get("rows", []):
            mint = r["mint"]
            all_history_mints.add(mint)
            cap_lamports = int(r["market_cap_lamports"])
            if cap_lamports <= 0:
                continue
            log_cap = math.log10(cap_lamports) - 9.0 # in SOL
            created_at = parse_iso(r["created_at"])

            if mint not in coin_data:
                coin_data[mint] = {
                    "symbol": r.get("symbol", mint[:6]),
                    "created_at": created_at,
                    "history": []
                }
            coin_data[mint]["history"].append((at_time, log_cap, r.get("venue", "curve")))

    history_until_sec = history_until.timestamp()

    # Compute features at cut
    coin_features = {}
    young_caps = [] # caps of coins observed in their first 3 hours

    for mint, data in coin_data.items():
        hist = data["history"]
        if not hist:
            continue
        # filter history up to history_until
        past_hist = [h for h in hist if h[0].timestamp() <= history_until_sec]
        if not past_hist:
            continue

        last_entry = past_hist[-1]
        last_cap = last_entry[1]
        last_time = last_entry[0].timestamp()

        created_sec = data["created_at"].timestamp()
        age_hours = max(0.1, (history_until_sec - created_sec) / 3600.0)

        if age_hours <= 3.0:
            young_caps.append(last_cap)

        peak_cap = max(h[1] for h in past_hist)
        # slope over recent marks
        recent = past_hist[-max(1, len(past_hist)//3):]
        if len(recent) >= 2:
            dt = (recent[-1][0].timestamp() - recent[0][0].timestamp()) / 3600.0
            slope = (recent[-1][1] - recent[0][1]) / dt if dt > 0 else 0.0
        else:
            slope = 0.0

        # volatility
        if len(past_hist) >= 3:
            changes = [past_hist[i][1] - past_hist[i-1][1] for i in range(1, len(past_hist))]
            mean_c = sum(changes) / len(changes)
            vol = math.sqrt(sum((c - mean_c)**2 for c in changes) / len(changes))
        else:
            vol = 0.15

        graduated = any(h[2] == "pool" for h in past_hist)

        coin_features[mint] = {
            "symbol": data["symbol"],
            "last_cap": last_cap,
            "age_hours": age_hours,
            "peak_cap": peak_cap,
            "slope": slope,
            "vol": max(0.05, vol),
            "graduated": graduated,
            "score": last_cap + 0.1 * slope
        }

    if not young_caps:
        young_caps = [2.0, 2.5, 3.0]

    # Select candidates (top ~150 by score)
    sorted_coins = sorted(coin_features.items(), key=lambda x: x[1]["score"], reverse=True)
    candidates = [mint for mint, feat in sorted_coins[:150]]

    # Monte Carlo over reads
    rng = random.Random(20261112)
    n_draws = 2000

    # Initialize accumulation structures for each read index (0 to 6)
    # place_counts[r][place_1_to_5][mint] = count
    # place_sizes[r][place_1_to_5][bin_0_81] = count

    read_place_counts = [ {k: {} for k in range(1, 6)} for _ in reads ]
    read_place_sizes = [ {k: {} for k in range(1, 6)} for _ in reads ]
    read_top5_counts = [ {} for _ in reads ]

    for r_idx, read_time in enumerate(reads):
        h_hours = max(0.1, (read_time.timestamp() - history_until_sec) / 3600.0)

        for draw in range(n_draws):
            draw_caps = {}
            # Sample candidate caps
            for mint in candidates:
                feat = coin_features[mint]
                # drift shrinks slope toward decay (-0.05 per hour for old/peaked)
                drift = 0.1 * feat["slope"]
                mu = feat["last_cap"] + drift * h_hours
                sigma = feat["vol"] * math.sqrt(h_hours) + 0.05
                sampled_cap = rng.gauss(mu, sigma)
                draw_caps[mint] = sampled_cap

            # Sample newcomers (Poisson number based on h_hours and history birth rate)
            # Roughly 5-10 newcomers per hour in active periods
            newcomer_lambda = 8.0 * h_hours
            n_newcomers = rng.poisson(newcomer_lambda) if hasattr(rng, 'poisson') else int(rng.expovariate(1.0 / newcomer_lambda) if newcomer_lambda > 0 else 0)
            n_newcomers = max(0, min(50, int(rng.gauss(newcomer_lambda, math.sqrt(newcomer_lambda)))))

            for i in range(n_newcomers):
                # draw cap from young_caps
                base_c = rng.choice(young_caps)
                # growth for newcomer since birth
                nc_cap = base_c + rng.gauss(0.2, 0.4) * h_hours
                nc_mint = f"newcomer_{r_idx}_{draw}_{i}"
                draw_caps[nc_mint] = nc_cap

            # Rank all by sampled cap descending
            ranked_draw = sorted(draw_caps.items(), key=lambda x: x[1], reverse=True)
            top_draw = ranked_draw[:5]

            for place_idx, (mint, cap) in enumerate(top_draw):
                place = place_idx + 1
                if not mint.startswith("newcomer_"):
                    read_place_counts[r_idx][place][mint] = read_place_counts[r_idx][place].get(mint, 0) + 1

                # bin for sizes
                bin_idx = 0
                if cap >= 6.0:
                    bin_idx = 81
                elif cap < 2.0:
                    bin_idx = 0
                else:
                    # edges
                    for b_i, edge in enumerate(edges):
                        if cap < edge:
                            bin_idx = b_i
                            break
                    else:
                        bin_idx = 81

                read_place_sizes[r_idx][place][bin_idx] = read_place_sizes[r_idx][place].get(bin_idx, 0) + 1

            if top_draw:
                top1_mint = top_draw[0][0]
                if not top1_mint.startswith("newcomer_"):
                    read_top5_counts[r_idx][top1_mint] = read_top5_counts[r_idx].get(top1_mint, 0) + 1

    # Aggregate across reads or average
    # Newcomer share prior per place (approx 0.25 to 0.45)
    newcomer_shares = {1: 0.60, 2: 0.50, 3: 0.50, 4: 0.50, 5: 0.50}

    probabilities_rows = []
    # To determine global top5, sum scores across reads
    aggregate_mint_scores = {}
    for r_idx in range(len(reads)):
        for place in range(1, 6):
            for mint, count in read_place_counts[r_idx][place].items():
                prob = count / n_draws
                aggregate_mint_scores[mint] = aggregate_mint_scores.get(mint, 0.0) + prob * (6 - place)

    top5_mints = sorted(aggregate_mint_scores.keys(), key=lambda m: aggregate_mint_scores[m], reverse=True)[:5]
    # Ensure 5 distinct mints from history
    while len(top5_mints) < 5 and candidates:
        for c in candidates:
            if c not in top5_mints:
                top5_mints.append(c)
                break
    top5_mints = top5_mints[:5]

    # Build probabilities with persistence blending
    last_mark_rows = marks[-1].get("rows", [])[:5] if marks else []
    last_mark_mints = [r["mint"] for r in last_mark_rows]

    for place in range(1, 6):
        nu = newcomer_shares[place]
        place_agg = {}
        for r_idx in range(len(reads)):
            for mint, count in read_place_counts[r_idx][place].items():
                place_agg[mint] = place_agg.get(mint, 0) + count

        total_place_draws = len(reads) * n_draws

        pers_dict = {}
        if place - 1 < len(last_mark_mints):
            pers_dict[last_mark_mints[place - 1]] = 0.30
        if place > 1 and place - 2 < len(last_mark_mints):
            pers_dict[last_mark_mints[place - 2]] = pers_dict.get(last_mark_mints[place - 2], 0.0) + 0.10
        if place < 5 and place < len(last_mark_mints):
            pers_dict[last_mark_mints[place]] = pers_dict.get(last_mark_mints[place], 0.0) + 0.10

        combined_mints = set(place_agg.keys()).union(pers_dict.keys())
        place_rows_map = {}
        is_short_history = len(marks) <= 4
        for mint in combined_mints:
            mc_freq = place_agg.get(mint, 0) / total_place_draws
            mc_p = mc_freq * (1.0 - nu)
            pers_p = pers_dict.get(mint, 0.0) * (1.0 - nu)

            if is_short_history:
                p_val = pers_p
            else:
                p_val = 0.4 * mc_p + 0.6 * pers_p
            if p_val >= 0.0001:
                place_rows_map[mint] = p_val

        sorted_place_coins = sorted(place_rows_map.items(), key=lambda x: x[1], reverse=True)[:100]
        for mint, p_val in sorted_place_coins:
            probabilities_rows.append({
                "mint": mint,
                "place": place,
                "p": round(p_val, 6)
            })

    # Build sizes
    sizes_rows = []
    for place in range(1, 6):
        bin_counts = {}
        for r_idx in range(len(reads)):
            for b_idx, count in read_place_sizes[r_idx][place].items():
                bin_counts[b_idx] = bin_counts.get(b_idx, 0) + count

        total_b_draws = len(reads) * n_draws
        size_mass = 0.95 # leave 5% unnamed size mass
        for b_idx, count in bin_counts.items():
            p_bin = (count / total_b_draws) * size_mass
            if p_bin >= 0.0001:
                sizes_rows.append({
                    "place": place,
                    "bin": int(b_idx),
                    "p": round(p_bin, 6)
                })

    forecast = {
        "probabilities": probabilities_rows,
        "top5": top5_mints,
        "sizes": sizes_rows
    }

    output_path = os.path.join(output_dir, "forecast.json")
    with open(output_path, "w") as f:
        json.dump(forecast, f, indent=2)

    print(f"Successfully generated forecast at {output_path}")

if __name__ == "__main__":
    main()
