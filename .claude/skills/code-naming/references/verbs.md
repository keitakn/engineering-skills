# 動詞の使い分け

関数・メソッドの先頭に置く動詞を選ぶための一覧。同じ意味に複数の語が挙がっている場合は、**プロジェクト内でどれか1つに統一する**。同じ動作を `fetch` と `load` で呼び分けると、読み手は違いがあると誤解する。

## 値を得る

`get` は使わない。何をして値を得るのかを書く（SKILL.md 規則1）。

| 動詞 | 意味 | 例 |
|---|---|---|
| `load` | 保存先（ファイル・データベース・ディスク）から読み込む | `loadAccount` |
| `fetch` | ネットワーク越しに取りに行く | `fetchAccount` |
| `read` | ストリーム・バッファから読む | `readLine` |
| `find` | 条件に合うものを探す。見つからない場合がある | `findById` |
| `search` | 探索する。複数件が返る | `searchArticles` |
| `list` | 一覧をまとめて返す | `listActiveUsers` |
| `count` | 件数を数える | `countUnread` |
| `calculate` / `compute` | 計算して求める | `calculateTotalPrice` |
| `build` / `compose` | 材料から組み立てる | `buildQuery` |
| `resolve` / `determine` | 複数の材料や規則から1つに決める | `resolveTimezone` |
| `extract` | 中から一部を取り出す | `extractDomain` |
| `select` | 候補から選ぶ | `selectNextTask` |
| `peek` | 先頭を取り出すが取り除かない | `peekJob` |

## 作る

| 動詞 | 意味 | 例 |
|---|---|---|
| `create` | 新しく作る | `createAccount` |
| `new` | 新しく作る（言語が許す場合） | `newAccount` |
| `from` | 既存のデータから作る | `fromConfig` |
| `of` | 値そのものから作る | `of` |
| `clone` / `copy` | 複製する | `cloneNode` |
| `init` | 初期化する（`initial` は「初期の」を表す形容詞） | `initDatabase` |

## 保存する・変える

| 動詞 | 意味 | 例 |
|---|---|---|
| `save` | 保存する | `saveAccount` |
| `store` | 保管する | `storeToken` |
| `insert` | 新しい行・要素として書き込む | `insertNotification` |
| `update` | 既存のものを書き換える | `updateAccount` |
| `upsert` | 無ければ作り、あれば書き換える | `upsertPushToken` |
| `commit` | 変更を確定する | `commitChange` |
| `apply` | 変更を反映する | `applySettings` |
| `flush` | 溜めていたものを送り出す | `flushBuffer` |
| `sync` | 別の場所と一致させる | `syncContacts` |

## 消す

| 動詞 | 意味 | 例 |
|---|---|---|
| `delete` | 永続化されたものを消す | `deleteAccount` |
| `remove` | コレクションから取り除く | `removeMember` |
| `clear` | 中身を空にする | `clearCache` |
| `reset` | 初期状態に戻す | `resetForm` |
| `purge` / `sweep` | 不要になったものをまとめて消す | `purgeStaleTokens` |

`delete` と `remove` を同じ意味で混ぜない。どちらか一方を「永続層からの削除」に割り当てる。

## 変換する

| 動詞 | 意味 | 例 |
|---|---|---|
| `to〜` | 別の形にして返す | `toString` / `toIsoDate` |
| `convert` | 形式を変換する | `convertCurrency` |
| `parse` | 文字列を構造に変える | `parseUrl` |
| `serialize` / `deserialize` | 直列化する・戻す | `serializePayload` |
| `format` | 表示用に整える | `formatAmount` |
| `normalize` | 表記を揃える | `normalizeEmail` |
| `map` | 要素ごとに変換する | `mapToDto` |

## 検査する

| 動詞 | 意味 | 例 |
|---|---|---|
| `validate` | 正しいかを検査し、不正なら例外またはエラーを返す | `validateInputs` |
| `ensure` | 期待する状態かを検査し、満たさなければ例外を返す。または満たすように整える | `ensureCapacity` |
| `assert` | 前提が成り立つことを確かめる | `assertInitialized` |
| `verify` | 正当性を照合する | `verifySignature` |

`check` は使わない。真のときに何が起きるのかが名前から読めない（SKILL.md 規則5）。真偽値を返すなら `references/booleans.md` の形にする。

## 非同期・スケジューリング

| 動詞・接尾辞 | 意味 | 例 |
|---|---|---|
| `schedule` / `post` | 実行するものをキューに積む | `scheduleJob` |
| `execute` / `start` | 実行を開始する | `executeTask` |
| `cancel` / `stop` | 実行を止める | `cancelJob` |
| `〜Async` | 非同期版であることを示す | `sendAsync` |
| `〜Sync` | 非同期版がある場合の同期版 | `sendSync` |
| `blocking〜` | 呼び出し元を待たせる | `blockingLoadUser` |
| `〜InBackground` | 裏で実行する | `syncInBackground` |
| `await` / `join` | 完了を待つ | `awaitCompletion` |

## コールバック

いつ呼ばれるかを名前に入れる。

| 接頭辞 | 意味 | 例 |
|---|---|---|
| `on〜` | 何かが起きたときに呼ばれる | `onCompleted` |
| `before〜` / `pre〜` / `will〜` | 何かが起きる直前に呼ばれる | `beforeUpdate` |
| `after〜` / `post〜` / `did〜` | 何かが起きた直後に呼ばれる | `afterSave` |
| `should〜` | 起こしてよいかを問い合わせる | `shouldUpdate` |

`will` / `did` の組と `before` / `after` の組を混在させない。

## コレクション操作

| 動詞 | 意味 |
|---|---|
| `add` / `append` | 末尾に加える |
| `insert` | 位置を指定して加える |
| `put` | キーに対応づけて加える |
| `remove` | 取り除く |
| `contains` | 含むかを判定する |
| `enqueue` / `dequeue` | 待ち行列の末尾に加える・先頭から取り出す |
| `push` / `pop` | スタックの先頭に積む・取り出す |
| `filter` | 条件に合うものだけを残す |
| `reduce` / `aggregate` | 畳み込んで1つにする |

## 条件付き実行

| 語 | 意味 | 例 |
|---|---|---|
| `〜IfNeeded` | 必要なときだけ実行し、不要なら何もしない | `redrawIfNeeded` |
| `try〜` | 実行を試み、失敗時は例外またはエラーを返す | `tryCreate` |
| `〜OrDefault` | 失敗時は既定値を返す | `findOrDefault` |
| `〜OrElse` | 失敗時は引数で渡された値を返す | `findOrElse` |
| `force〜` | 通常なら止まる条件でも強行する | `forceStop` |
| `〜Once` | 一度しか実行しない | `warnOnce` |

## 状態を切り替える

| 動詞 | 意味 |
|---|---|
| `enable` / `disable` | 操作できる状態にする・できない状態にする |
| `activate` / `deactivate` | 有効な状態にする・無効な状態にする |
| `show` / `hide` | 表示する・隠す |
| `lock` / `unlock` | 施錠する・解錠する |
| `mark〜` | 印を付ける（`markAsRead`） |

`toggle` は避ける。呼んだ後の状態が引数から決まらず、追跡が難しくなる。`turnOn` / `turnOff` のように、結果の状態を名前で決める。
