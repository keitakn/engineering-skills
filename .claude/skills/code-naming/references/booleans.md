# 真偽値の命名

## 判定基準

**`if` の条件に置いて、英文として読めるかどうか。** `== true` を付けないと文にならない名前は直す。

```
if (hasPersonalityId)      読める
if (checkPersonalityId)    読めない（真のとき何が成り立つのか分からない）
```

## 使ってよい6つの型

| 型 | 何を表すか | 例 |
|---|---|---|
| `is` + 形容詞 | 今の状態 | `isEmpty` / `isVisible` / `isValid` |
| `is` / `has` + 過去分詞 | 済んでいるかどうか | `isChanged` / `isDeleted` / `hasSent` |
| `can` + 動詞の原形 | できるかどうか | `canUpdate` / `canMove` |
| `should` + 動詞の原形 | すべきかどうか | `shouldRetry` / `shouldCache` |
| `needs` + 名詞／動詞 | 必要かどうか | `needsMigration` |
| 三人称単数の動詞（＋名詞） | 成り立つかどうか | `exists` / `contains` / `existsError` |

意味の違いを使い分ける。

- `is` — 対象が期待する状態になっているか
- `has` — 対象がそのデータや性質を持っているか
- `can` — 対象がその動作を行えるか
- `should` — 呼び出し側がその動作を行ったほうがよいか
- `needs` — 呼び出し側がその動作を行う必要があるか

`can` と `should` は判断の主体が違う。`can` は対象の能力、`should` は呼び出し側への助言。

## 使わない3つの型

| 避ける形 | 理由 | 直し方 |
|---|---|---|
| `check` + 名詞 | 真のときに何が成り立つのか読めない | `isValid` / `exists` / `hasError` |
| `is` + 動詞の原形 | 英文にならない | `isFail` → `isFailed` |
| 動詞の原形 + 名詞 | 三人称単数になっていない | `existError` → `existsError` |

## 否定形を名前にしない

`isNotValid` のような名前は、否定条件と組み合わせたときに二重否定になり、読み違えを生む。

```
if (!isNotValid)     読み違えやすい
if (isValid)         読める
```

肯定形で定義し、必要な側で否定する。ただし、対象そのものの性質が「無い」ことである場合（`isEmpty` / `isMissing`）は肯定形として扱ってよい。

## 有効・無効を表す語

| 語 | 意味 |
|---|---|
| `valid` / `invalid` | 内容が正しいか（`isValidEmail` / `invalidMessage`） |
| `enabled` / `disabled` | 操作できる状態にあるか |
| `active` / `inactive` | 稼働しているか |
| `available` | 利用できる状態にあるか |
| `required` / `optional` | 必須か任意か |

## 進行と完了

| 形 | 意味 | 例 |
|---|---|---|
| 現在分詞（`〜ing`） | 進行中 | `isLoading` / `isWaiting` / `isPending` |
| 過去分詞（`〜ed`） | 完了済み | `isSelected` / `isWarned` / `isCompleted` |

進行中と完了済みを1つの真偽値で兼ねない。3つ以上の状態があるなら、真偽値ではなく状態を表す列挙型にする。
