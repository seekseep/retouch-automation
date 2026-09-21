# XMP の仕様

T7 が書き出す XMP サイドカーの書式をここにまとめる。

**Lightroom の実物と照合したものだけを「確定」に置く。** 推測は必ず「未確定」に置き、
何が分かれば決まるかを書く。確定したら該当項目をこちらへ移し、根拠を残す。

## 検証に使った実物

| 項目 | 値 |
| --- | --- |
| 元ファイル | `DSC02759.ARW`（SONY ILCE-7C, 3936 x 2624） |
| 生成物 | Lightroom で編集 → メタデータをファイルに保存した `DSC02759.xmp` |
| `crs:Version` | `18.1` |
| `crs:ProcessVersion` | `15.4` |
| `x:xmptk` | `Adobe XMP Core 7.0-c000` |
| 実物の保管場所 | `samples/xmp-check/lightroom-truth.xmp`（git 追跡外） |

Lightroom 側で行った操作は 3 つだけ。

1. 切り抜きの角度補正に **`+5.00`** を入力（画面上では画像が**時計回り**に 5 度回った。
   画像枠の上辺が右下がりになり、背景パネルの支柱も上が右へ倒れた）
2. 切り抜き枠を縮小（結果は左右・上下とも中央に揃った。後述）
3. キーワードに `いけばな > テスト` を入力

---

## 確定した項目

### サイドカーの置き場所と名前

RAW と同じディレクトリに `<RAW のベース名>.xmp`。`DSC02759.ARW` → `DSC02759.xmp`。

### 全体構造

```xml
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="...">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    ...
   crs:HasCrop="True"
   crs:CropAngle="-5"
   ...>
   <dc:subject>
    <rdf:Bag>
     <rdf:li>...</rdf:li>
    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
```

`crs:` はすべて `rdf:Description` の**属性**として書かれる。
`dc:subject` だけが子要素になる。

### `crs:CropAngle` の符号 ← 確定

**内部表現（CCW 正）をそのまま書く。符号は反転させない。**

根拠：

| | |
| --- | --- |
| Lightroom UI の角度補正 | `+5.00` |
| そのときの画面上の回転 | **時計回り** 5 度 |
| XMP に出た値 | `crs:CropAngle="-5"` |

時計回り 5 度は CCW 正の表記では -5 度。XMP の値も -5。よって**同符号**。

実装は [`geometry.CROP_ANGLE_SIGN`](../src/retouch_automation/geometry.py) に 1 箇所だけ置いてある。
値は `1.0`。反転が必要になったらここだけを直す。

単位は度。Lightroom は `-5` のように小数点以下を省いて書くが、
小数を書いても読めている（自前の出力を Lightroom が解釈できるかは別途確認）。

### `dc:subject`（キーワード）の書式 ← 確定

```xml
<dc:subject>
 <rdf:Bag>
  <rdf:li>いけばな &gt; テスト</rdf:li>
 </rdf:Bag>
</dc:subject>
```

`rdf:Bag` の中に `rdf:li` を並べる。T7 が既に書いている形と一致している。

### `crs:HasCrop`

`"True"`（文字列。`true` や `1` ではない）。

### `crs:Crop*` の座標定義 ← 確定

**軸に平行な外接矩形ではない。** 傾いたトリミング枠の**向かい合う 2 頂点**を、
回転前の画像座標で表して正規化したもの。

    (CropLeft × W,  CropTop × H)     … 枠の一方の頂点
    (CropRight × W, CropBottom × H)  … 対角の頂点

枠そのものは `CropAngle` だけ傾いている。枠の実寸は、この 2 頂点の差分ベクトルを
`-CropAngle` だけ回すと出る。角度が 0 のときだけ軸平行の矩形に退化する。

書くときは逆をやる。「回転後の画像で見た軸平行の矩形」の対角 2 頂点を、
画像中心まわりに `-angle` 回して回転前の座標に直し、W と H で割る。
実装は [`geometry.image_to_crop_norm()`](../src/retouch_automation/geometry.py)。

#### 根拠

同じ写真で 2 通りの切り抜きを Lightroom に作ってもらい、
XMP と書き出した JPEG の両方を突き合わせた。
JPEG は元の ARW と SIFT で特徴点対応を取り、アフィン変換を推定して
「元画像のどこがどう切り抜かれたか」を実測した（インライア 489/511）。

**1 枚目：角度 +5.00、縦横比 1x1**

| | |
| --- | --- |
| XMP | `CropAngle="-5"` `CropLeft="0.208353"` `CropTop="0.13292"` `CropRight="0.791647"` `CropBottom="0.86708"` |
| 実測 | 一辺 2119.2 px の正方形が画像中心にあり、5 度傾いている |

素直に「幅・高さの比率」と読むと 2295.8 × 1926.4 の長方形（縦横比 1.19）になり、
正方形にならない。差分ベクトル (2295.8, 1926.4) を **+5 度**回すと
ちょうど **2119.2 × 2119.2** になる。

逆向きに、一辺 2119.2 の正方形の対角 2 頂点を -(-5) 度回して正規化すると

    (820.08, 348.78) → CropLeft 0.208353, CropTop 0.13292
    (3115.92, 2275.22) → CropRight 0.791647, CropBottom 0.86708

と、XMP の値が小数以下まで一致する。

**2 枚目：角度 0、右上寄りの 1x1**

| | |
| --- | --- |
| XMP | `CropAngle="0"` `CropLeft="0.461584"` `CropTop="0"` `CropRight="1"` `CropBottom="0.807622"` |
| 画素に直すと | 2119.2 × 2119.2 の正方形 |

角度 0 なので軸平行の矩形に退化し、素直な比率として読める。
`Top=0` / `Right=1` は Lightroom が枠を画像の端に吸着させたもの。

この 2 つは [tests/test_geometry.py](../tests/test_geometry.py) に回帰テストとして入れてある。

### 自作 XMP の往復検証 ← 確定

こちらが書いた XMP を Lightroom に読ませ、書き出した JPEG を元の ARW と
照合して「意図どおりに反映されるか」を実測した。

手順は、`ROUNDTRIP_TEST.ARW` と同名の XMP を並べて置き、Lightroom に
**新規読み込み**させ、何も編集せずに JPEG を書き出す。
JPEG は SIFT で元画像と対応を取り、アフィン変換から回転と切り抜き位置を求める
（インライア 450/484）。

指示した内容：回転は内部表現 +3.0 度（反時計回りに 3 度回して水平にする）、
切り抜きは回転後の画像で 左 20% 上 10% 右 70% 下 75%。

| 項目 | 実測 | 指示 | 差 |
| --- | --- | --- | --- |
| 回転 | 反時計回り 3.006 度 | 反時計回り 3.000 度 | 0.006 度 |
| 枠 左 | 0.2010 | 0.2000 | 0.10% |
| 枠 上 | 0.1032 | 0.1000 | 0.32% |
| 枠 右 | 0.6975 | 0.7000 | 0.25% |
| 枠 下 | 0.7480 | 0.7500 | 0.20% |
| 縦横比 | 1.1538 | 1.1527 | 0.1% |

誤差はすべて測定側の精度の範囲。キーワードも `ikebana` と `roundtrip_test` の
両方がキーワード欄に出た。

**クラウド版の Lightroom（Adobe Lightroom 9.1）がサイドカー XMP を読む**ことも
これで確認できた。Classic 専用の機能ではない。

### 取り込み済みの写真への反映 ← 確定（ただし再起動が要る）

**Lightroom は動作中にサイドカーを読み直さない。再起動すると読む。**

同じ `ROUNDTRIP_TEST` の XMP に現像設定を足して、切り抜きと回転はそのままにして
書き出しを比べた。

| | 書き出し | 編集パネル |
| --- | --- | --- |
| XMP を書き換えた直後 | 前回と**画素単位で同一**（最大差 0） | 全スライダー 0 |
| Lightroom 再起動後 | 変化あり（44.8% の画素） | 反映されている |

XMP 側が Lightroom に上書きされることは無かった（`x:xmptk="retouch-automation"` のまま）。
無視されていただけ。

運用上は「XMP を作ってから読み込む」順にしておけば、この再起動は要らない。

### 現像設定の反映 ← 確定

`crs:Highlights2012="-100"` / `crs:Shadows2012="+100"` を入れて書き出しを比べた。

| | 現像なし | 現像あり | 差 |
| --- | --- | --- | --- |
| 暗部 1%点 | 7.0 | 21.0 | +14.0 |
| 暗部 5%点 | 20.0 | 43.0 | +23.0 |
| 明部 99%点 | 205.0 | 182.0 | -23.0 |

暗部が持ち上がり明部が落ちている。指示どおりに効いている。
`+100` / `-100` は測りやすさのための極端な値で、実際の T6 は
`Shadows` 最大 +20、`Highlights` 最大 -40 に抑えてある。

### 階層キーワードは使わない

`lr:hierarchicalSubject` は書かない。キーワードは `dc:subject` に平坦に並べる方針。
分類グループは `artwork_0001` のような単独のタグとして付ける。

---

## 未確定の項目

### 取り込み済み写真への反映

Lightroom に取り込み済みの写真に対して、後から XMP を置いて反映できるかは未確認。
新規読み込み時に読むこととは別の話として扱う。

---

## 気をつけること

### 画像寸法が rawpy と Lightroom で食い違う

| 取得元 | 寸法 |
| --- | --- |
| Lightroom の `tiff:ImageWidth` / `tiff:ImageLength` | 3936 x 2624 |
| rawpy の `postprocess()` | 3968 x 2648 |

**32 x 24 画素（約 0.8%）ずれている。** 有効画素の切り出し方が LibRaw と Adobe で違うため。

`crs:Crop*` は 0.0–1.0 の比率なので、どちらの寸法で正規化するかで結果が約 0.8% ずれる。
いまの T7 は rawpy 側の寸法で正規化している。作品を切り落とす向きに効く可能性があるので、
`crs:Crop*` の座標定義を確定させるときに併せて詰めること。

### レンズプロファイル補正が入っている

実物には `crs:LensProfileEnable="1"` と
`crs:LensProfileFilename="SONY (Sony FE 28-60mm F4-5.6) - RAW.lcp"` が入っていた。
歪曲補正は画像の実効的な画角を変えるので、`crs:Crop*` の基準寸法を詰めるときは
これが効いていないか確認すること。

### `crs:CropConstrainToUnitSquare`

実物では `"1"`。名前からして切り抜き矩形を単位正方形に収める制約に見えるが、
何を単位正方形と呼んでいるのかは未確認。`crs:Crop*` の基準を詰めるときに一緒に見る。

---

## いま T7 が書いている XMP

```xml
<x:xmpmeta xmlns:crs="..." xmlns:dc="..." xmlns:rdf="..." xmlns:x="adobe:ns:meta/"
           x:xmptk="retouch-automation">
 <rdf:RDF>
  <rdf:Description rdf:about=""
    crs:HasCrop="True"
    crs:CropTop="0.090300" crs:CropLeft="0.297000"
    crs:CropBottom="0.847200" crs:CropRight="0.769200"
    crs:CropAngle="0.2542"
    crs:Exposure2012="+0.00" ... crs:RawFileName="DSC02760.ARW">
   <dc:subject><rdf:Bag>
    <rdf:li>ikebana</rdf:li><rdf:li>artwork_0002</rdf:li>
   </rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
```

既存の XMP がある場合は上書きせずマージする（既定は `xmp.on_existing = "skip"`）。

### 実物と比べて足りていない可能性があるもの

Lightroom の実物には以下も入っていた。無くても読めるのか、
無いと既定値に落ちるのかは未確認。

- `crs:HasSettings="True"`
- `crs:ProcessVersion` / `crs:Version`
- `crs:CropConstrainToUnitSquare` / `crs:CropConstrainToWarp`
- `crs:WhiteBalance="As Shot"`
