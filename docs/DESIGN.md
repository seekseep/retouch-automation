# 設計：ディレクトリ構成・共通インターフェース・開発順序

[SPEC.md](./SPEC.md) を実装に落とすための設計。仕様側で「確認してから実装せよ」とされている事項は、
ここでは **未確定（要検証）** と明記し、確定したらこの文書を更新する。

## 0. 名前と実行方法

- リポジトリ / コマンド：`retouch-automation`
- Python パッケージ：`retouch_automation`
- 実行：`retouch-automation process /path/to/photos`

## 1. ディレクトリ構成

```
retouch-automation/
├── SPEC.md                   # 要求仕様（原文）
├── DESIGN.md                 # この文書
├── README.md                 # インストール・モデル準備・実行方法・既知の制約
├── pyproject.toml
├── src/retouch_automation/
│   ├── __init__.py
│   ├── __main__.py           # python -m retouch_automation
│   ├── cli.py                # 引数解釈、サブコマンド
│   ├── config.py             # 設定ファイル（余白量・しきい値・モデル指定）
│   ├── models.py             # 共通データモデル（Pydantic）★ツール間の契約
│   ├── geometry.py           # 座標系変換の一元管理 ★事故が起きる場所
│   ├── workspace.py          # 作業ディレクトリ、中間成果物、再実行判定
│   ├── report.py             # 処理レポート
│   ├── orchestrator.py       # T8
│   └── tools/
│       ├── base.py           # 各ツールの Protocol 定義
│       ├── raw_loader.py         # T1 RAW読み込み
│       ├── classifier.py         # T2 写真分類
│       ├── artwork_detector.py   # T3 作品検出
│       ├── straightener.py       # T4 水平推定
│       ├── cropper.py            # T5 トリミング
│       ├── developer.py          # T6 レタッチ
│       └── xmp_writer.py         # T7 XMP生成
├── tests/
└── scripts/
    └── probe_lightroom_xmp.py   # Lightroom 実物 XMP の観察用
```

作業ディレクトリ（既定 `<raw_dir>/.retouch-automation/`）：

```
.retouch-automation/
├── photos/DSC00001/
│   ├── preview.jpg        # 解析用画像
│   ├── mask.png           # 作品マスク
│   ├── result.json        # PhotoResult の永続化（再実行判定に使う）
│   └── overlay/           # 確認用画像（マスク・基準線・トリミング枠）
├── groups.json            # 分類結果
└── report.json / report.md
```

RAW ディレクトリに書き込むのは `*.xmp` のみ。

## 2. 座標系（最重要）

4 つを別の型として扱い、素の `tuple` で持ち回らない。変換は `geometry.py` だけが知る。

| 空間 | 定義 | 原点・単位 |
| --- | --- | --- |
| `SENSOR` | RAW の有効画素領域。EXIF Orientation 適用**前** | px、左上 |
| `IMAGE` | Orientation 適用後の「見た目どおり」の画像 | px、左上 |
| `PREVIEW` | 解析用に縮小した画像。全ての画像解析はここで動く | px、左上 |
| `CROP_NORM` | XMP に書く正規化座標 | 0.0–1.0（**定義は要検証**） |

規則：

- 画像解析（T3/T4/T5/T6）は **`PREVIEW` でのみ** 行う。
- `PREVIEW → IMAGE` はスケール 1 本。`IMAGE → SENSOR` は Orientation の逆適用。
- `IMAGE → CROP_NORM` は **未確定**。Lightroom 実物 XMP との照合後に確定する。
- 回転は座標空間を増やさず、「角度 + 回転中心」として持ち回り、必要な時に適用する。
- `crs:CropAngle` の符号は **未確定**。`geometry.to_crop_angle()` に 1 箇所で閉じ込め、検証結果で反転できるようにする。

## 3. 共通インターフェース

全ツールは「1 枚の `PhotoContext` を受け取り、自分の担当フィールドを埋めた結果を返す」形に揃える。
モデルを差し替えても他が壊れないよう、具体的なモデル名（Florence-2 / SAM / DINOv2）は
`tools/*.py` の内側に閉じ込め、`models.py` には出さない。

```python
class Tool(Protocol):
    name: str
    def prepare(self) -> None: ...      # モデルのロード。全写真で 1 回
    def release(self) -> None: ...      # メモリ解放
```

各ツールの署名（`models.py` の型のみに依存）：

| ツール | 署名 |
| --- | --- |
| T1 | `load(raw_path) -> RawImage` |
| T3 | `detect(RawImage) -> ArtworkDetection` |
| T2 | `group(list[RawImage], list[ArtworkDetection]) -> dict[photo_id, Classification]` |
| T4 | `estimate(RawImage, ArtworkDetection) -> Straightening` |
| T5 | `compute(RawImage, ArtworkDetection, Straightening) -> Crop` |
| T6 | `develop(RawImage, ArtworkDetection, Classification) -> Develop` |
| T7 | `write(PhotoResult, xmp_path, mode) -> None` |

T2 だけが全写真を横断する（グループ化のため）。よって T8 は
**T1 → T3 を全写真ぶん回す → T2 を 1 回 → T4/T5/T6 を全写真ぶん回す → T7** の順で動かす。

### ステータスの扱い

全ての部分結果は `status` を持つ。

- `success` … 正常
- `fallback` … 検出できず保守的な既定値を採用（処理は継続、レポートに要確認として記録）
- `failed` … 当該ツールが失敗（他は継続。XMP は可能な範囲で生成）

`T1` の `failed` だけは例外で、その写真は XMP を生成せず失敗として扱う。

## 4. 開発順序

契約（`models.py` + `geometry.py` + T7）さえ固まれば、T2〜T6 は互いに独立して実装できる。
各ツールがフォールバックを持つので、他が未完成でもパイプラインは最後まで通る。
直列なのは最初の 2 つだけで、そこから先は並列に進められる。

```
Phase 0  検証（前提を確定させる。ここが崩れると設計が変わる）
   ↓
Phase 1  契約と骨格（models / geometry / T1 / T7 / T8 / CLI）
   ↓
   ├── T3 作品検出   Florence-2 + SAM 2.1
   ├── T4 水平推定   OpenCV（モデル不要）
   ├── T5 トリミング  純粋な幾何計算（モデル不要）
   ├── T2 分類       DINOv2
   └── T6 レタッチ    RAW 現像との突き合わせ
```

### Phase 0：検証スパイク（実装より前）

1. **Lightroom の XMP 実物を取る** — 1 枚の ARW を Lightroom で開き、回転・トリミング・露出・キーワードを
   手で付け、生成された XMP を入手する。`crs:CropAngle` の符号、`crs:Crop*` の座標定義、
   `dc:subject` の書式、必須の補助項目を **実物から** 確定する。
2. **Lightroom クラウドへの反映経路** — 「新規読み込み時に XMP を読む」と
   「取り込み済みの写真に後から XMP を反映する」を **別々に** 検証する。
   後者が不可なら、運用を「XMP 生成 → その後で Lightroom に読み込む」順に固定する必要がある。
3. **モデルの Apple Silicon 対応** — Florence-2 / SAM 2.1 / DINOv2 の
   バージョン・取得元・MPS 実行可否・CPU フォールバックを実際に動かして確認する。

### Phase 1：契約と骨格（モデル非依存）

`models.py` / `geometry.py` / `workspace.py` / `report.py` / T1 / T7 / T8 / CLI。
T2〜T6 は全て `fallback` を返す実装で埋めておく。

→ **この時点で「RAW ディレクトリを指定すると全 RAW に XMP が出る」が通る。**
以降は各ツールがフォールバックを実装に置き換えていくだけになる。

### Phase 2 以降：並列

| ツール | 依存 | 並列可否 |
| --- | --- | --- |
| T4 水平推定 | OpenCV のみ | すぐ着手可 |
| T5 トリミング | 幾何計算のみ | すぐ着手可 |
| T3 作品検出 | Florence-2 + SAM 2.1 | すぐ着手可 |
| T2 分類 | DINOv2 | すぐ着手可 |
| T6 レタッチ | RAW 現像の比較環境 | すぐ着手可 |

T3 のマスクは T2 / T4 / T5 の **精度を上げる** 入力であって、着手の前提ではない。
T3 が未完成のうちは各ツールがフォールバック（画像全体）を受け取って動くので、
T3 の完成を待たずに並行して進められる。

## 5. 依存関係の方針

重い ML 依存（torch / transformers 等）は optional extra に分け、Phase 1–2 は入れずに動くようにする。

- コア：`pydantic` `numpy` `opencv-python` `rawpy` `Pillow` `lxml`
- extra `models`：`torch` `transformers` その他（バージョンは Phase 0 の確認後に固定）

## 5.5 T3 の classical バックエンドについて（実写で確定したこと）

モデル導入前のつなぎとして classical バックエンドを置いているが、
**当初の GrabCut ベースは展示写真では使えない**ことが実 ARW 9 枚で分かった。

- 展示写真は作品の背後に無地の幕が張ってあり、画面の大半を幕が占める。
  GrabCut は「中央にある高コントラストの塊」を取るので、作品ではなく
  **幕そのもの**を拾う。9 枚すべてで発生し、1 枚では隣の展示まで枠に入った。
- さらに GrabCut には実用上の問題が 3 つあった。
  - 一様に近い画像で GMM が縮退し、`cv2.grabCut` が C++ 側のループから戻らない。
    Python のシグナルも届かず SIGKILL 以外で止められない（グレースケール標準偏差
    0 で再現。0.5〜0.8 でも 2.5〜4.2 秒かかる）。
  - GMM の初期化が OpenCV のグローバル RNG を引くため、**同じ写真でも
    プロセス内の呼び出し順で結果が変わる**（同一入力 8 回中 1 回が崩れた）。
  - 2048 px のプレビューで 1 枚あたり約 40 秒。

そこで classical は「幕の色からの距離 ＋ 幕には無い細かい濃淡」に置き換えた。
幕の色は中央領域の Lab 中央値で推定する（作品より幕のほうが広いので中央値が幕になる）。
彩度ではなく色差で測るのは、灰色の陶器のように彩度の低い花器を落とさないため。

実写 9 枚での結果：6 枚は作品にほぼ一致、3 枚は枠が幕全体に広がる。
ただし 3 枚とも**作品は枠の内側に収まっており切れてはいない**。
最終確認は人が Lightroom で行う前提なので、広すぎる側に外れるのは許容する。

Florence-2 + SAM 2.1 を入れたらこの classical は置き換わる。

## 5.6 T1 で踏んだ座標と時刻の取り違え（実写で確定したこと）

**`image_size` を `raw.sizes.width/height` から取ってはいけない。**
これは回転を適用する前の値で、`postprocess()` は Orientation を適用した向きで返す。
縦位置の写真（`sizes.flip = 5`）では 3968x2648 と 2648x3968 が食い違い、
`preview_scale` が 1.5 倍ずれ、`CROP_NORM` も縦横が入れ替わる。
プレビューの元になった配列の実寸から取ること。

**撮影日時はファイルの更新時刻で代用してはいけない。**
T2 は撮影間隔で作品の切れ目を見るので、コピーや書き出しで全ファイルが同じ時刻に
なると全写真が 1 つの作品に束ねられる（実際に踏んだ）。
`rawpy` の `raw.other.timestamp` に実際の撮影時刻がある。
同じ `other` から ISO・シャッター速度・絞りも取れる。

## 5.7 Florence-2 が使えなかったこと（実機で確定）

設計では T3 の位置推定に Florence-2 を想定していたが、**この環境では読み込めない**。

`microsoft/Florence-2-base` / `-large` / `-base-ft` / `-large-ft` の 4 つとも、
`AutoProcessor.from_pretrained` が
`AttributeError: RobertaTokenizer has no attribute image_token` で落ちる。
公開されているのは旧 remote-code 形式のままのリポジトリで、
transformers 5 系が持つ native 実装（`Florence2Processor`）が要求する
`image_token` を tokenizer が持っていないため。変換済みの公開リポジトリも見当たらない。

代わりに **GroundingDINO**（`IDEA-Research/grounding-dino-base`）を使う。
「テキストで指示した物体の位置を出す」という仕事は同じで、transformers 5 に
native 実装がある。SPEC は Florence-2 を「まずは検討してください」と書いており、
モデルを差し替えても他のツールが壊れない設計を求めているので、この置き換えは
`config.models.detector_model` を変えるだけで済む。

### 実機で確認したこと（Apple Silicon / MPS）

| | バージョン | 取得元 | MPS | 備考 |
| --- | --- | --- | --- | --- |
| torch | 2.14.0 | PyPI | 可 | `torch.backends.mps.is_available()` が True |
| transformers | 5.17.0 | PyPI | — | Dinov2 / Florence2 / Sam2 を native に持つ |
| 位置推定 | GroundingDINO base 232M | `IDEA-Research/grounding-dino-base` | 可 | 約 2 秒/枚 |
| 領域抽出 | SAM 2.1 base-plus 73M | `facebook/sam2.1-hiera-base-plus` | 可 | 約 4.5 秒/枚 |
| 特徴量 | DINOv2 base 87M | `facebook/dinov2-base` | 可 | 768 次元 |

CPU フォールバックは `modelzoo.resolve_device()` が一手に引き受ける。
`device` に `auto` / `mps` / `cuda` のどれを指定しても、使えなければ黙って CPU に落ちる。
モデル依存そのものが入っていない環境では、`backend: auto` が classical に落ちて全工程が通る。

SAM 2.1 の重みは `sam2_video` 型なので `Sam2Model` に読ませると
「アーキテクチャの一部だけを読み込む」旨の警告が出る。画像 1 枚の推論は問題なく通る。

## 5.8 モデルを入れて分かったこと（実写）

### T3：位置推定の確信度が低いとマスクが退化する

GroundingDINO の確信度が 0.3 前後の写真では、SAM 2.1 が枠の中の細部だけを拾って
マスクが面積比 0.005 まで痩せた。これをそのまま採用すると classical より悪い結果で
上書きしてしまうので、面積比が想定外なら「モデルでは取れなかった」として
classical に譲るようにした。実写 16 枚で検出の fallback が 6 件から 0 件に減った。

### T3：枠は「主枠に重なるものだけ」を統合する

プロンプトを `a flower arrangement. a vase. flowers. branches.` のように
複数フレーズにすると、隣に並んだ別の展示まで `branches` として拾う。
確信度が最大の枠を主にして、そこに**重なる**枠だけを足す。
展示写真では主枠が `a vase`（0.768）で、そこに重なる花や枝の枠を足して
作品全体になった。

### T2：DINOv2 で初めて分類が成立した

展示写真 13 枚・6 作品で校正した。連続する 12 区間の類似度は

| | 類似度 | 撮影間隔 |
| --- | --- | --- |
| 同一作品内（7 区間） | 0.9730 〜 0.9900 | 1 〜 5 秒 |
| 別作品の境（5 区間） | 0.0207 〜 0.4446 | 6 〜 22 秒 |

あいだが **0.53** 空く。しきい値 0.70 と `TIME_GAP_SECONDS = 15` の組み合わせで、
13 枚が正解どおり 6 作品に分かれた（混在ゼロ）。

**時刻だけでは切り分けられない。** 作品の切れ目が 6 秒しか空かないことがあり、
同一作品内の最大 5 秒と 1 秒差しかない。時刻は「見た目が似ていても間が空いていれば
別の作品」を拾う歯止めに徹し、切り分けの主役は類似度が担う。

色ヒストグラム（classical）では同じ性質の写真で境界のマージンが 0.2% しか無かった。
**モデル無しでは T2 は実質的に動かない。**

### 校正に使えない写真がある

`~/Desktop/20260801_夏季講習` の 130 枚は、暗い会場でのステージ記録
（登壇者・観客のシルエット・製作中の大型装花）で、
このシステムが対象とする「白い壁を背にした個別の作品」とは別種だった。
T3 が探す「1 つの作品」がそもそも写っていない写真が多く、校正には使えない。
一時期この 130 枚から `TIME_GAP_SECONDS` を決めていたが、根拠として不適切だった。

### 埋め込み JPEG は Orientation の適用が要る

埋め込みサムネイルは 1920x1080 で、**回転前のデータに EXIF Orientation タグが
付いた形**で入っている（縦位置の写真で Orientation=8）。
一方 `rawpy.postprocess()` は回転を適用した向きで返す。
`preview.use_embedded_jpeg` を有効にするなら Orientation を自分で適用すること。
なお 1920x1080 は解析用プレビューの既定（長辺 2048）より小さい。


## 6. 未確定事項（Phase 0 で確定させる）

- [x] `crs:CropAngle` の符号と単位 — **確定**。単位は度、内部表現（CCW 正）と同符号。
      根拠と検証手順は [xmp.md](./xmp.md)
- [x] `crs:CropTop/Left/Bottom/Right` の座標定義 — **確定**。
      軸平行の外接矩形ではなく、傾いた枠の向かい合う 2 頂点を
      回転前の画像座標で正規化したもの。根拠は [xmp.md](./xmp.md)
- [ ] `crs:HasCrop` と併せて必要な補助項目
      ※ 往復検証は `HasCrop` / `Crop*` / `CropAngle` / `dc:subject` だけで通った。
        Lightroom の実物にある `HasSettings` / `ProcessVersion` / `Version` /
        `CropConstrainToUnitSquare` は無くても読まれる。
- [x] `dc:subject` の書式 — **確定**。`rdf:Bag` に `rdf:li` を並べる。T7 の出力と一致。
- ~~階層キーワード（`lr:hierarchicalSubject`）の書式~~ — **対象外**。
      キーワードは階層にしない方針。`dc:subject` に平坦に並べる（書式は確定済み）。
- [ ] 画像寸法の食い違い（Lightroom 3936x2624 / rawpy 3968x2648、約 0.8%）
      ※ `crs:Crop*` は比率なので、中心が一致していれば端で最大 0.4%（全解像度で
        約 16 px）ずれるだけ。構図余白 12% に対して十分小さいので当面は許容する。
- [x] 新規読み込み時に XMP を読むか — **確定**。クラウド版 Lightroom 9.1 が
      切り抜き・回転・キーワードを読んだ。往復検証の数値は [xmp.md](./xmp.md)
- [x] **取り込み済み**写真への後追い反映 — **確定**。動作中は読み直さないが、
      Lightroom を再起動すると読む。XMP が上書きされることは無い。
- [x] 現像設定（`Highlights2012` / `Shadows2012`）の反映 — **確定**。
      暗部 +23 / 明部 -23（輝度 0–255）で指示どおり効いた
- [x] モデルのバージョンと MPS 可否 — **確定**。5.7 の表を参照。
      Florence-2 は読み込めないため GroundingDINO に置き換えた。
- [x] 埋め込み JPEG と RAW 現像結果の向き — **確定**。埋め込みは回転前 + Orientation タグ、
      postprocess は回転適用済み。使うなら自分で Orientation を適用する。5.8 参照
- [ ] プレビュー解像度（細い枝の検出に必要な下限）
- [x] `TIME_GAP_SECONDS` と `MODEL_SIMILARITY_THRESHOLD` の校正 — **確定**。
      展示写真 13 枚・6 作品で実測。5.8 参照
- [ ] classical バックエンドが幕全体に広がる 3 枚の条件（照明が暗く幕が
      ベージュ系のとき。幕の縁のコントラストが上がるのが原因と見ている）
