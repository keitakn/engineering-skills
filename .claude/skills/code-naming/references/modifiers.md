# 修飾語

名前の中心となる名詞・動詞に添えて、時間・位置・状態・確からしさを補う語。

## 時間軸

| 語 | 意味 | 例 |
|---|---|---|
| `current` | 現在の | `currentUser` |
| `previous` | 直前の | `previousValue` |
| `next` | 次の | `nextPage` |
| `first` / `last` | 最初の・最後の | `firstItem` / `lastError` |
| `latest` | 最新の | `latestRelease` |
| `original` | 変更前の元の値 | `originalPrice` |

`last` は「最後の」と「直前の」の両方に読める。直前を表したいときは `previous` を使う。

## 出来事との前後

| 語 | 意味 | 例 |
|---|---|---|
| `before` / `pre` / `will` | 起きる前 | `beforeCreate` |
| `after` / `post` / `did` | 起きた後 | `afterLeave` |
| `on` | 起きたとき | `onCompleted` |

## 階層・関係

| 語 | 意味 |
|---|---|
| `parent` / `child` | 親・子 |
| `sibling` | 兄弟 |
| `ancestor` / `descendant` | 祖先・子孫 |
| `root` / `leaf` | 根・葉 |
| `owner` | 所有者 |
| `related` | 関連する |
| `source` / `target` | 変換や移動の元・先 |

## 状態

動詞の形を変えて状態を表す。

| 形 | 意味 | 例 |
|---|---|---|
| 現在分詞（`〜ing`） | 進行中 | `loading` / `waiting` / `pending` |
| 過去分詞（`〜ed`） | 完了済み | `selected` / `warned` / `expired` |
| `able`（`〜able`） | できる性質を持つ | `readable` / `draggable` |

## 条件・結果

| 語 | 意味 | 例 |
|---|---|---|
| `If〜` | 条件を満たすときだけ実行する | `warnIfUnsupported` |
| `With〜` | それを伴って行う | `closeWithError` |
| `Without〜` | それを除いて行う | `nameWithoutWhitespace` |
| `As〜` | その形にして返す | `readAsText` |
| `By〜` | それを手がかりにする | `findByEmail` |
| `For〜` | その対象に向けたもの | `templateForGuest` |
| `From〜` / `To〜` | 元・先 | `convertFromCsv` / `toIsoString` |

## 数量・過不足

| 語 | 意味 | 例 |
|---|---|---|
| `total` | 合計 | `totalPrice` |
| `remaining` | 引いた後の残り | `remainingWidth` |
| `extra` | 必須ではない追加分 | `extraHeaders` |
| `missing` | 必須なのに欠けているもの | `missingDependencies` |
| `duplicated` | 重複しているもの | `duplicatedKeys` |
| `max` / `min` | 上限・下限 | `maxRetries` |
| `default` | 既定値 | `defaultTimezone` |

## 確からしさ

| 語 | 意味 | 例 |
|---|---|---|
| `maybe` | 期待した値でない可能性がある | `maybeId` |
| `raw` | 加工前のまま | `rawResponse` |
| `estimated` | 見積もり値 | `estimatedDuration` |
| `SoFar` | 途中経過であり、後で変わる | `pathSoFar` |

## 注意喚起

危険な操作は、名前を長くしてでも危険であることを明示する。呼び出し側が読み飛ばせない名前にするため。

| 語 | 意味 | 例 |
|---|---|---|
| `dangerous〜` | 誤用すると壊れる | `dangerouslyReplaceState` |
| `force〜` | 通常は止まる条件でも強行する | `forceDelete` |
| `unsafe〜` | 安全性の検査を省いている | `unsafeParse` |
| `deprecated〜` | 使用を推奨しない | — |

## 初期化

| 語 | 品詞 | 意味 |
|---|---|---|
| `init` | 動詞 | 初期化する（`initDatabase`） |
| `initial` | 形容詞 | 初期の（`initialContext`） |

動詞と形容詞を取り違えない。`initialize` と `initial` を同じ意味で混ぜない。

## 区切り・その他

| 語 | 意味 | 例 |
|---|---|---|
| `separator` / `delimiter` | 区切りに使うもの | `pathSeparator` |
| `prefix` / `suffix` | 前に付ける・後ろに付ける | `filePrefix` |
| `threshold` | 判定の境目 | `staleThreshold` |
| `once` | 一度だけ | `warnOnce` |
