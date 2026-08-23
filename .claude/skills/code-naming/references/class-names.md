# クラス・インタフェースの命名

## 基本

- クラス・型は名詞にする。そのクラスが「何であるか」を表す。
- インタフェースは、性質を表す形容詞（`Iterable` / `Closeable` / `Comparable`）か、役割を表す名詞（`〜Repository` / `〜Sender`）にする。
- 抽象度の高い広い名詞は、あらゆる関心事のロジックを引き寄せる。何の関心事を扱うのかを名前に含める（SKILL.md 規則3）。

## 役割を表す語

役割が決まっている語を使うと、クラス名だけで責務の範囲が伝わる。

### 外部とやり取りする

| 語 | 役割 | 例 |
|---|---|---|
| `Client` | 外部サービスとの通信の窓口 | `PaymentApiClient` |
| `Gateway` | 外部システムへの出入口を1か所にまとめる | `TimelineGateway` |
| `Repository` | 永続化されたデータの出し入れ | `UserRepository` |
| `Store` / `Storage` | 保存先そのもの | `FavoriteSettingStore` |
| `Registry` | 登録されたものを引くための台帳 | `HandlerRegistry` |
| `Cache` | 一時的に保持して再利用する | `TimelineCache` |
| `Sender` / `Publisher` | 送り出す | `PushNotificationSender` |
| `Receiver` / `Subscriber` | 受け取る | `WebhookReceiver` |

### データを加工する

| 語 | 役割 | 例 |
|---|---|---|
| `Filter` | 条件で絞り込む | `TimelineFilter` |
| `Extractor` | 中から取り出す | `MessageExtractor` |
| `Formatter` | 表示用に整える | `AmountFormatter` |
| `Collector` | 集めてまとめる | `NotificationCandidateCollector` |
| `Converter` / `Mapper` | 別の型に変換する | `UserDtoMapper` |
| `Validator` | 正しさを検査する | `EmailValidator` |
| `Builder` | 段階的に組み立てる | `QueryBuilder` |
| `Factory` | 生成の手順をまとめる | `SessionFactory` |

### 判断する

| 語 | 役割 | 例 |
|---|---|---|
| `Specification` | 条件を満たすかを判定する規則そのもの | `DeliveryWindowSpecification` |
| `Policy` | 選択の方針を保持する | `RetryPolicy` |
| `Rule` | 個別の規則 | `PasswordStrengthRule` |
| `Resolver` | 複数の材料から1つを決める | `TimezoneResolver` |
| `Strategy` | 差し替え可能な手順 | `PricingStrategy` |

### 実行する

| 語 | 役割 | 例 |
|---|---|---|
| `Job` / `Task` | 実行単位 | `UploadJob` |
| `Runner` / `Executor` / `Worker` | 実行する側 | `UploadJobRunner` |
| `Scheduler` | いつ実行するかを決める | `BatchScheduler` |
| `Handler` / `Dispatcher` | 受け取って処理を割り振る | `EventDispatcher` |
| `Listener` / `Watcher` | 変化を監視する | `ClickListener` |

### 記録・設定

| 語 | 役割 | 例 |
|---|---|---|
| `Logger` | 記録を書き出す | `AuditLogger` |
| `Log` / `History` | 記録されたもの | `UsageHistory` |
| `Configuration` / `Setting` / `Preference` | 設定値 | `DeliveryConfiguration` |
| `Migrator` | 版の違いを吸収して移し替える | `UserDataMigrator` |

### まとめる

| 語 | 役割 | 例 |
|---|---|---|
| `Facade` | 複雑な内部を1つの窓口に隠す | `CheckoutFacade` |
| `Provider` | 取得元を隠して値を提供する | `TimelineProvider` |
| `Adapter` | 合わない型どうしをつなぐ | `LegacyApiAdapter` |

## 避けるクラス名

| 避ける名前 | 何が起きるか | 直し方 |
|---|---|---|
| `〜Manager` | 「管理する」の範囲が定まらず、あらゆる処理が集まって肥大する | 実際にする動作ごとに分ける（`〜Repository` / `〜Factory` / `〜Cleaner`） |
| `〜Util` / `〜Helper` | どこにも属さない関数の置き場になり、際限なく増える | 対象の型を冠したクラスにするか、その型自身のメソッドにする |
| `〜Info` / `〜Data` / `〜Item` | 接尾辞を外しても意味が通ることが多い。通らないなら中身が定まっていない | 外せるなら外す。外せないなら何の情報かを名前にする |
| `〜Base` / `Abstract〜` / `Common〜` | 継承関係の都合を名前にしており、何であるかを表していない | そのクラスが表す概念を名詞で書く |
| `Service` | フレームワークが定義する `Service` と意味が衝突しやすい | フレームワークの用語として使われている場合のみ使い、それ以外は役割語を選ぶ |

`Controller` は、MVC のように枠組みが役割を定めている場合は使ってよい。枠組みの外で「制御するもの」の意味で使うと `Manager` と同じ問題が起きる。

## 対応するクラス名を揃える

インタフェースと実装、あるいは対になる型は、語幹を揃える。

- `PushNotificationSender`（インタフェース） / `FcmPushNotificationSender`（実装）
- 実装側には、何で実現しているかを接頭辞に付ける。`〜Impl` は何で実現しているかを語らないため使わない。
