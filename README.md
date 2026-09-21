# retouch-automation

生花・いけばな作品の RAW 写真を一括処理し、各 RAW と同名の XMP サイドカーを書き出す。

RAW ファイルは変更・移動・削除しない。写真の採用・不採用は判断せず、全写真を処理する。
最後に人間が Lightroom で分類タグを使って重複を外す。

- 要求仕様：[docs/SPEC.md](./docs/SPEC.md)
- 設計と開発順序：[docs/DESIGN.md](./docs/DESIGN.md)
- XMP の書式（Lightroom と照合した結果）：[docs/xmp.md](./docs/xmp.md)

## 現在の状態

全ツールを実装し、実 ARW で通しを確認した状態。
XMP の書式（`crs:CropAngle` の符号、`crs:Crop*` の座標定義、`dc:subject`）は
Lightroom の実物と照合して確定済み（[docs/xmp.md](./docs/xmp.md)）。

| ツール | 状態 |
| --- | --- |
| T1 RAW読み込み | rawpy でプレビュー生成。撮影日時・ISO・絞りは EXIF から取る |
| T2 写真分類 | DINOv2 の特徴量＋撮影間隔。実写 13 枚・6 作品で正解と一致 |
| T3 作品検出 | GroundingDINO で位置、SAM 2.1 で領域（モデル無しなら色差＋質感へ退避） |
| T4 水平推定 | LSD。水平線と垂直線の一致で確信度を出す |
| T5 トリミング | マスク優先。端寄りは全画面へ倒す |
| T6 レタッチ | 白飛び・黒つぶれの張り付きから Highlights / Shadows のみ |
| T7 XMP生成 | マージ方式。書式は Lightroom の実物と照合済み |
| T8 オーケストレーター | モデルを 1 回読んで全写真で使い回す |

## インストール

```sh
uv sync --python 3.12 --extra models
```

`--extra models` を省くと torch / transformers を入れずに動く。その場合 T2 と T3 は
モデルを使わないバックエンドへ自動的に落ちる（精度は落ちるが全工程は通る）。

### モデルの準備

初回実行時に Hugging Face から自動で取得する（合計 1.5GB 程度、`~/.cache/huggingface` に入る）。

| 用途 | モデル | 大きさ |
| --- | --- | --- |
| T3 位置推定 | `IDEA-Research/grounding-dino-base` | 232M |
| T3 領域抽出 | `facebook/sam2.1-hiera-base-plus` | 73M |
| T2 特徴量 | `facebook/dinov2-base` | 87M |

Apple Silicon では MPS を使う。使えない環境では自動的に CPU へ落ちる。
`models.device` に `cpu` を指定すれば固定できる。

設計では T3 の位置推定に Florence-2 を想定していたが、公開されているリポジトリが
transformers 5 系の native 実装と噛み合わず読み込めないため GroundingDINO に置き換えた。
詳細は [docs/DESIGN.md](./docs/DESIGN.md) の 5.7。

## 実行

```sh
uv run retouch-automation process /path/to/photos
```

| オプション | 内容 |
| --- | --- |
| `--config <json>` | 設定ファイル。余白量やしきい値を上書きする |
| `--overwrite-xmp` | 既存 XMP を丸ごと置き換える（Lightroom が書いた EXIF 等も失われる） |

**既定は `skip` で、既存の XMP があれば触らない。** 人が Lightroom で直した
回転・トリミングを再実行で消さないため。ツールを改良して全部作り直したいときだけ
`--overwrite-xmp` を付けるか、設定で `{"xmp": {"on_existing": "merge"}}` を指定する。

出力：

- `<raw_dir>/*.xmp` … RAW と同名のサイドカー
- `<raw_dir>/.retouch-automation/` … プレビュー・マスク・中間 JSON・確認用画像・レポート

## 設定

`config.py` の各セクションがそのまま JSON のキーになる。

```json
{
  "crop": { "composition_margin_ratio": 0.12 },
  "straighten": { "max_abs_angle": 8.0 },
  "xmp": { "base_tags": ["ikebana"], "on_existing": "skip" }
}
```

## テスト

```sh
uv run pytest
```

## 既知の制約

- **Lightroom は動作中にサイドカーを読み直さない。** 取り込み済みの写真に後から
  XMP を置いた場合、反映には Lightroom の再起動が要る。
  「XMP 生成 → そのあと読み込む」順にしておけば再起動は不要。
- **Lightroom クラウドの取り込み済み写真に、後から XMP を反映できるかは未確認。**
  新規読み込み時に XMP を読むこととは別の話として扱う。
- モデル（Florence-2 / SAM 2.1 / DINOv2）は未導入。Apple Silicon での動作は未確認。
- 対応 RAW は Sony ARW のみ。
- 遠近補正は対象外。単純な回転補正のみ。
- **T3 は GrabCut を使っていない。** 展示写真では作品の背後の無地の幕を拾ってしまい、
  実写 9 枚すべてで失敗した（1 枚では隣の展示まで枠に入った）。
  幕の色からの距離と、幕には無い細かい濃淡で代替している。詳細は [docs/DESIGN.md](./docs/DESIGN.md) の 5.5。
- **明るい壁や柱が作品の近くにあると枠が広がることがある。** 画面を端から端まで
  貫く成分は落としているが、画面の 9 割程度で止まる光の筋などは残る。
  この場合 T5 が全画面へ倒すので、作品が切れることはない。
- **モデル無しでは分類が実質的に動かない。** 作品の切れ目は 6 秒しか空かないことが
  あり、撮影間隔だけでは切り分けられない。色ヒストグラムでは同一作品と別作品の
  マージンが 0.2% しか無かった。`--extra models` を入れること。
- 位置推定の確信度が低い写真ではマスクが退化するため、モデルを使わない
  バックエンドへ自動的に譲る。
