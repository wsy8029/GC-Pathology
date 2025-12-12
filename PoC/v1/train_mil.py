import argparse
import json
import random
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    roc_auc_score,
)
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm


FEATURE_KEYS = ["features", "feats", "embedding", "embeddings", "feat"]
COORD_KEYS = ["coords", "coord", "locs", "locations"]


def load_feats_coords(data):
    import re

    feature_keys = ["features", "feats", "embedding", "embeddings", "feat", "last_layer_embed"]
    feats = None
    for k in feature_keys:
        if k in data:
            feats = data[k]
            break
    if feats is None:
        layer_keys = [k for k in data.keys() if re.match(r"^layer_\d+_embed$", k)]
        if layer_keys:
            best = sorted(layer_keys, key=lambda x: int(re.findall(r"\d+", x)[0]))[-1]
            feats = data[best]
    coords = None
    for k in COORD_KEYS:
        if k in data:
            coords = data[k]
            break
    if feats is None:
        raise KeyError(f"feature key not found in pt; available keys={list(data.keys())}")
    return feats, coords


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def compute_metrics(y_true, y_prob, threshold=0.5):
    y_true = np.array(y_true)
    y_prob = np.array(y_prob)
    y_pred = (y_prob >= threshold).astype(int)
    metrics = {}
    try:
        metrics["auc"] = float(roc_auc_score(y_true, y_prob))
    except Exception:
        metrics["auc"] = float("nan")
    try:
        metrics["ap"] = float(average_precision_score(y_true, y_prob))
    except Exception:
        metrics["ap"] = float("nan")
    metrics["f1"] = float(f1_score(y_true, y_pred)) if len(y_true) else float("nan")
    metrics["acc"] = float(accuracy_score(y_true, y_pred)) if len(y_true) else float("nan")
    try:
        tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    except Exception:
        tn = fp = fn = tp = float("nan")
    metrics["confusion"] = {"tn": float(tn), "fp": float(fp), "fn": float(fn), "tp": float(tp)}
    return metrics


class SlideBagDataset(Dataset):
    def __init__(self, manifest_path: Path, split: str, max_tiles=None, shuffle_tiles=False):
        df = pd.read_csv(manifest_path)
        self.df = df[df["split"] == split].reset_index(drop=True)
        self.max_tiles = max_tiles
        self.shuffle_tiles = shuffle_tiles

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        row = self.df.iloc[idx]
        data = torch.load(row["pt_path"], map_location="cpu")
        feats, coords = load_feats_coords(data)
        if isinstance(coords, np.ndarray):
            coords = torch.from_numpy(coords)
        feats = feats.float()
        if coords is not None:
            coords = coords.int()
        if self.shuffle_tiles:
            perm = torch.randperm(len(feats))
            feats = feats[perm]
            if coords is not None:
                coords = coords[perm]
        if self.max_tiles and len(feats) > self.max_tiles:
            idxs = torch.randperm(len(feats))[: self.max_tiles]
            feats = feats[idxs]
            if coords is not None:
                coords = coords[idxs]
        label = torch.tensor(float(row["label"]), dtype=torch.float32)
        return feats, coords, label, row["slide_id"]


def collate_fn(batch):
    feats, coords, labels, slide_ids = zip(*batch)
    labels = torch.tensor(labels, dtype=torch.float32)
    return list(feats), list(coords), labels, list(slide_ids)


class AttentionMIL(nn.Module):
    def __init__(self, in_dim, hidden_dim=256, dropout=0.2, gated=False):
        super().__init__()
        self.gated = gated
        self.input_norm = nn.LayerNorm(in_dim)
        self.dropout = nn.Dropout(dropout)
        self.attn_v = nn.Linear(in_dim, hidden_dim)
        self.attn_u = nn.Linear(in_dim, hidden_dim) if gated else None
        self.attn_out = nn.Linear(hidden_dim, 1)
        self.classifier = nn.Linear(in_dim, 1)

    def forward(self, feats, coords=None):
        x = self.dropout(self.input_norm(feats))
        v = torch.tanh(self.attn_v(x))
        if self.gated:
            u = torch.sigmoid(self.attn_u(x))
            v = v * u
        attn_score = self.attn_out(v).squeeze(-1)
        weight = torch.softmax(attn_score, dim=0)
        pooled = torch.sum(weight.unsqueeze(-1) * x, dim=0)
        logit = self.classifier(pooled).squeeze(0)
        return logit, weight


class TransMIL(nn.Module):
    def __init__(self, in_dim, depth=2, heads=4, ff_dim=512, dropout=0.1, use_pos_enc=False):
        super().__init__()
        self.use_pos_enc = use_pos_enc
        self.input_norm = nn.LayerNorm(in_dim)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=in_dim, nhead=heads, dim_feedforward=ff_dim, dropout=dropout, batch_first=True
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=depth)
        self.cls_token = nn.Parameter(torch.randn(1, 1, in_dim) * 0.02)
        self.cls_bias = nn.Parameter(torch.zeros(1, 1, in_dim))
        self.pos_mlp = nn.Sequential(nn.LayerNorm(2), nn.Linear(2, in_dim)) if use_pos_enc else None
        self.classifier = nn.Linear(in_dim, 1)

    def forward(self, feats, coords=None):
        feats = self.input_norm(feats)
        x = feats.unsqueeze(0)
        if self.use_pos_enc and coords is not None and coords.shape[0] > 1:
            pos = coords.float()
            pos_std = pos.std(0, keepdim=True)
            pos_std = torch.where(pos_std > 1e-6, pos_std, torch.ones_like(pos_std))
            pos = (pos - pos.mean(0, keepdim=True)) / pos_std
            x = x + self.pos_mlp(pos).unsqueeze(0)
        cls = self.cls_token + self.cls_bias
        tokens = torch.cat([cls, x], dim=1)
        out = self.encoder(tokens)
        cls_out = out[:, 0]
        logit = self.classifier(cls_out).squeeze(1)
        attn_score = torch.matmul(out[:, 1:], cls_out.unsqueeze(-1)).squeeze(-1)
        weight = torch.softmax(attn_score, dim=-1)
        return logit.squeeze(0), weight.squeeze(0)


def infer_in_dim(manifest_path: Path):
    df = pd.read_csv(manifest_path)
    if df.empty:
        raise RuntimeError("manifest is empty")
    sample_pt = Path(df.iloc[0]["pt_path"])
    data = torch.load(sample_pt, map_location="cpu")
    feats, _ = load_feats_coords(data)
    return int(feats.shape[1])


def make_loaders(manifest_path, max_tiles, num_workers):
    train_ds = SlideBagDataset(manifest_path, "train", max_tiles=max_tiles, shuffle_tiles=True)
    val_ds = SlideBagDataset(manifest_path, "val", max_tiles=max_tiles, shuffle_tiles=False)
    test_ds = SlideBagDataset(manifest_path, "test", max_tiles=max_tiles, shuffle_tiles=False)

    def _loader(ds, shuffle):
        if len(ds) == 0:
            return None
        return DataLoader(ds, batch_size=1, shuffle=shuffle, num_workers=num_workers, collate_fn=collate_fn)

    return _loader(train_ds, True), _loader(val_ds, False), _loader(test_ds, False), train_ds, val_ds, test_ds


def find_best_threshold(y_true, y_prob, grid=None):
    grid = grid or np.arange(0.3, 0.71, 0.05)
    best_t, best_f1 = 0.5, -1
    for t in grid:
        pred = (np.array(y_prob) >= t).astype(int)
        f1 = f1_score(y_true, pred) if len(y_true) else float("nan")
        if np.isnan(f1):
            continue
        if f1 > best_f1:
            best_f1, best_t = f1, float(t)
    return best_t, best_f1


def run_split(model, loader, device, threshold):
    if loader is None:
        return {"auc": float("nan"), "ap": float("nan"), "f1": float("nan"), "acc": float("nan"), "confusion": {}}, [], [], []
    model.eval()
    y_true, y_prob, rows = [], [], []
    with torch.inference_mode():
        for feats, coords, labels, slide_ids in loader:
            feats = feats[0].to(device)
            coords = coords[0].to(device) if coords[0] is not None else None
            labels = labels.to(device)
            logit, attn = model(feats, coords)
            prob = torch.sigmoid(logit).item()
            y_true.append(float(labels.item()))
            y_prob.append(prob)
            rows.append({"slide_id": slide_ids[0], "prob": prob, "label": float(labels.item())})
    metrics = compute_metrics(y_true, y_prob, threshold=threshold)
    return metrics, rows, y_true, y_prob


def train(args):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    set_seed(args.seed)

    manifest = pd.read_csv(args.manifest)
    split_counts = manifest["split"].value_counts().to_dict()
    label_counts = manifest["label"].value_counts().to_dict()
    print(f"[단계] manifest 로드 ({args.manifest}) -> 총 {len(manifest)} 슬라이드")
    print("       split 분포:", split_counts)
    print("       라벨 분포:", label_counts)

    in_dim = infer_in_dim(args.manifest)
    if args.model == "transmil":
        model = TransMIL(
            in_dim,
            depth=args.depth,
            heads=args.heads,
            ff_dim=args.ffn_dim,
            dropout=args.dropout,
            use_pos_enc=args.use_pos_enc,
        )
    elif args.model == "clam":
        model = AttentionMIL(in_dim, hidden_dim=args.hidden_dim, dropout=args.dropout, gated=True)
    else:
        model = AttentionMIL(in_dim, hidden_dim=args.hidden_dim, dropout=args.dropout, gated=False)
    model = model.to(device)

    train_loader, val_loader, test_loader, train_ds, val_ds, test_ds = make_loaders(
        args.manifest, args.max_tiles, args.num_workers
    )
    if train_loader is None:
        raise RuntimeError("train split is empty")

    pos = float((manifest["label"] == 1).sum())
    neg = float((manifest["label"] == 0).sum())
    auto_pw = (neg + 1.0) / (pos + 1.0)
    pos_weight = torch.tensor([args.pos_weight if args.pos_weight > 0 else auto_pw], device=device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(args.epochs, 1))
    scaler = torch.cuda.amp.GradScaler(enabled=args.precision == "fp16")

    print(f"[단계] 데이터 크기 -> train {len(train_ds)} | val {len(val_ds)} | test {len(test_ds)}")
    print(f"[단계] {device}에서 {args.epochs} epoch 학습 시작 (precision {args.precision})")

    best_auc = -1.0
    best_path = Path(args.ckpt_dir) / "best.pt"
    Path(args.ckpt_dir).mkdir(parents=True, exist_ok=True)
    Path(args.logdir).mkdir(parents=True, exist_ok=True)
    no_improve = 0

    history = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        running_loss = 0.0
        optimizer.zero_grad(set_to_none=True)
        pbar = tqdm(train_loader, desc=f"train epoch {epoch}", ncols=100)
        for step, (feats, coords, labels, slide_ids) in enumerate(pbar, 1):
            feats = feats[0].to(device)
            coords = coords[0].to(device) if coords[0] is not None else None
            labels = labels.to(device)
            if args.label_smoothing > 0:
                eps = args.label_smoothing
                labels = labels * (1 - eps) + 0.5 * eps
            with torch.cuda.amp.autocast(enabled=args.precision == "fp16"):
                logit, attn = model(feats, coords)
                loss = criterion(logit.view_as(labels), labels)
                loss = loss / args.grad_accum
            scaler.scale(loss).backward()
            if step % args.grad_accum == 0:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
            running_loss += loss.item()
            pbar.set_postfix({"loss": running_loss / step})
        scheduler.step()

        val_metrics, _, y_true, y_prob = run_split(model, val_loader, device, args.threshold)
        history.append({"epoch": epoch, "val": val_metrics, "loss": running_loss / max(1, step)})
        print(f"epoch {epoch}: val_auc={val_metrics['auc']:.4f} f1={val_metrics['f1']:.4f}")

        if val_metrics["auc"] > best_auc:
            best_auc = val_metrics["auc"]
            torch.save({"model_state": model.state_dict(), "config": vars(args), "val_metrics": val_metrics}, best_path)
            print("=> 최고 성능 갱신, 체크포인트 저장:", best_path)
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= args.patience:
                print(f"[조기 종료] {args.patience} epoch 동안 개선 없음 → 중단")
                break

    best = torch.load(best_path, map_location=device)
    model.load_state_dict(best["model_state"])
    val_metrics, val_rows, val_true, val_prob = run_split(model, val_loader, device, args.threshold)
    best_thr, best_f1 = find_best_threshold(val_true, val_prob)
    val_best = compute_metrics(val_true, val_prob, threshold=best_thr)
    test_metrics, test_rows, test_true, test_prob = run_split(model, test_loader, device, best_thr)

    summary = {
        "best_val_auc": best_auc,
        "val": val_metrics,
        "val_best_threshold": best_thr,
        "val_best_metrics": val_best,
        "test": test_metrics,
        "history": history,
    }
    log_path = Path(args.logdir) / "mil_training.json"
    with open(log_path, "w") as f:
        json.dump(summary, f, indent=2)
    print("== 최종 성능 ==")
    print("  validation(default thr):", val_metrics)
    print("  validation(best thr):", val_best)
    print("  test (best thr from val):", test_metrics)
    print("best threshold (val f1):", best_thr)
    print("결과 저장:", log_path)
    if test_rows:
        pd.DataFrame(test_rows).to_csv(Path(args.logdir) / "test_preds.csv", index=False)
    if val_rows:
        pd.DataFrame(val_rows).to_csv(Path(args.logdir) / "val_preds.csv", index=False)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True)
    p.add_argument("--ckpt_dir", default="checkpoints")
    p.add_argument("--logdir", default="logs")
    p.add_argument("--model", choices=["abmil", "clam", "transmil"], default="abmil")
    p.add_argument("--hidden_dim", type=int, default=256)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--heads", type=int, default=4)
    p.add_argument("--ffn_dim", type=int, default=512)
    p.add_argument("--dropout", type=float, default=0.2)
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--weight_decay", type=float, default=1e-4)
    p.add_argument("--pos_weight", type=float, default=-1.0)
    p.add_argument("--label_smoothing", type=float, default=0.0)
    p.add_argument("--precision", choices=["fp16", "fp32"], default="fp16")
    p.add_argument("--grad_accum", type=int, default=2)
    p.add_argument("--max_tiles", type=int, default=None)
    p.add_argument("--num_workers", type=int, default=2)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--use_pos_enc", action="store_true")
    p.add_argument("--patience", type=int, default=10)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    train(args)
