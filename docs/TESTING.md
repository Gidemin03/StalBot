# Проверки по типу изменения

Команды выполняются из `C:\Users\demid\Documents\PythonProj\StalBot`.
Для чисто текстовых изменений проверьте факты, ссылки и команды; повторять весь OCR не нужно.

## Выбор проверки

| Изменение | Проверка | Условия |
|---|---|---|
| Правила, каталог, бюджет, scanner/runner/storage | `tests/test_bot.py` | Временные SQLite, fake desktop; без игры и весов OCR |
| Диапазоны заточки, поиск каталога и история | `tests/test_catalog_history.py`, `tests/test_ui.py`, `tests/test_validation.py`, `tests/test_bot.py` | Временные SQLite: миграция старой схемы, все границы 0..15, пересечения, неизвестная заточка; Tk: поиск, фильтры, сортировка ROI, отмена/очистка; сохранность бюджета и защит BUY после перезапуска. Oracle и Scanner проверяют диапазоны с fake OCR |
| Группы выбора, настройки, маршруты наблюдения/live и время | `tests/test_bot.py`, `tests/test_config.py`, `tests/test_runner_cycle.py` | Fake OCR/desktop; live-тест заменяет покупку заглушкой |
| Критерии интеграционного прогона | `tests/test_validation.py` | Защита от полного отказа числового OCR и подмены baseline |
| Tk UI бота | `tests/test_ui.py` | Windows desktop-сессия, Tcl/Tk; тест меняет системный текстовый clipboard |
| Календарь, вся история и сумма прогноза | `tests/test_date_picker.py`, `tests/test_ui.py`, `tests/test_catalog_history.py` | Tk-календарь без ручного ввода: високосность, месяцы/годы, выбор/сброс/закрытие; временная SQLite: более 200 строк, границы местного дня с микросекундами, точная сумма Decimal только SUCCESS, фильтры/сортировка/очистка |
| Глобальные F8/F9 | `tests/test_hotkeys.py`, `tests/test_ui.py` | Поддельный WinAPI и очередь; без SendInput и ввода в игру |
| Перенос/нажатие/отпускание мыши, отмена и настройки | `tests/test_windows.py`, `tests/test_config.py`, `tests/test_ui.py`, `tests/test_bot.py` | Fake WinAPI/время, временная БД; проверка порядка, фокуса, освобождения и запрета повторного BUY |
| Переключение «Курсором / Без курсора» | `tests/test_background_desktop.py`, `tests/test_windows.py`, `tests/test_config.py`, `tests/test_ui.py`, `tests/test_bot.py` | Fake WinAPI/время: PostMessage без курсора/фокуса, общие задержки, захват, отмена, отпускание при ошибке, закрепление окна и геометрия; сохранение настройки и передача адаптера во все экранные режимы. Без реальных кликов; полный цикл нового режима в игре проверяется отдельно |
| Отдельный PostMessage probe для второго монитора | `tests/test_background_click_probe.py` | Fake WinAPI: клиентские координаты при отрицательных экранных, ввод без фокуса, отпускание при отмене/ошибке, остановка при закрытии/сворачивании/смене размера. Реальных кликов нет; ручной запуск и ограничения — в README |
| Прогрев/повторное использование Reader и закрытие UI | `tests/test_ocr_session.py`, `tests/test_ui.py` | Поддельные native API/Reader; полный OCR отдельно |
| Рабочий точный NAME LRU | `tests/test_name_cache.py`, `tests/test_config.py`, `tests/test_ui.py`, `tools/validate_bot.py` | Точный RGB/shape, LRU/0, owner thread, прогрев, session reuse, свежие числа/каталог/бюджет; полный OCR и сравнение Scanner с кешем/без |
| Переключение CPU/GPU | `tests/test_gpu_reader.py`, `tests/test_ocr_session.py`, `tests/test_config.py`, `tests/test_ui.py`, `tests/test_runner_cycle.py` | Fake native: безопасные DML options, недоступный провайдер, отключённый runtime fallback, закрытие до смены, batch9, сохранение CPU batch |
| Проверка меню без BUY, Search/таймаут/отмена | `tests/test_input_probe.py` | Fake desktop, время и временная SQLite; реальных кликов нет |
| Стабилизация меню перед BUY в обоих режимах | `tests/test_background_purchase.py`, `tests/test_purchase_options.py`, `tests/test_bot.py`, `tests/test_recovery.py` | Стабильность 30 мс, меню 500 мс/опрос 15 мс, результат 1000 мс/опрос 40 мс, восстановление 1000 мс/опрос 15 мс/повтор клика 300 мс; смена цены и финальная проверка остаются блокирующими. Fake desktop/время, без покупок |
| Переключение changed/static и повторного OCR | `tests/test_input_probe.py`, `tests/test_purchase_options.py`, `tests/test_config.py`, `tests/test_ui.py` | Статический кадр/отмена, число OCR-вызовов, меню, бюджет, история, UNKNOWN, сохранение UI |
| Недостаточно средств и подробные метрики | `tests/test_insufficient.py`, `tests/test_purchase_metrics.py`, `tests/test_windows.py`, `tests/test_input_probe.py`, `tests/test_ui.py` | Реальный эталон/отрицательные примеры, fake часы/ввод, момент DOWN, распознавание, бюджет |
| Search, непрерывные циклы и региональный захват | `tests/test_search_refresh.py`, `tests/test_input_probe.py`, `tests/test_runner_cycle.py`, `tests/test_windows.py`, `tests/test_config.py` | Fake desktop/время: UNCHANGED без OCR/BUY, UNSTABLE, строки2/3/4/QTY, все9 при стабилизации, region/full, polling0, пауза/стоп |
| Неизвестное сообщение стандартного диалога | `tests/test_search_interface.py`, `tests/test_insufficient.py` | Настоящие эталоны + синтетически удалённый текст; блокировка ввода не классифицирует outcome |
| Восстановление покупки и неизвестный результат | `tests/test_recovery.py`, `tests/test_bot.py`, `tests/test_runner_cycle.py`, `tests/test_catalog_history.py`, `tests/test_purchase_options.py`, `tests/test_purchase_metrics.py` | Повтор OK, отмена, чистый экран, однократный резерв бюджета; запрет повторного BUY до нового Search, допуск одинаковой цены после обновления/перезапуска, сохранность аудита; только fake desktop |
| Прибыль за сессию и остановка по бюджету | `tests/test_bot.py`, `tests/test_runner_cycle.py`, `tests/test_ui.py` | Сумма SUCCESS, UNKNOWN без прибыли, стоп после BUY, сброс нового запуска; бюджет 0/49 999/50 000/None, резерв UNKNOWN, отсутствие нового Search после достижения порога |
| Builder | `dataset/test_builder.py` | Окружение Builder, Tk/clipboard; подробности в его README |
| Экспериментальные парсеры, preprocessing, resume | `benchmarks/test_*.py` | Окружение benchmark; без OCR inference |
| Прототипы оптимизации: кеш, workers, конвейер, GDI RGB, очередь диагностики | `benchmarks/test_optimization_*.py` | Fake workers/время, синтетические RGB и временные БД; actual OCR и latency отдельно, последовательными сериями из [benchmark README](../benchmarks/README.md#оптимизация-рабочего-reader-2026-10-16) |
| Рабочий OCR, цвет, batch, шаблоны | `tools/validate_bot.py` | Модели, изображения и согласованный baseline |

Unit-тесты ядра импортируют numpy/OpenCV/Pillow через vision, хотя не загружают веса.
В тестах непрерывных циклов и восстановления перед следующим Search бюджет
увеличен с 1 000 до 100 000 ₽, чтобы проверять прежнее поведение выше нового
порога остановки; отдельные тесты проверяют сам порог 50 000 ₽.
Tk smoke вызывает обработчики программно; это не полная проверка мышью и клавиатурой.
Тесты Search включают задержанный ответ 600/1200 мс, неизменившиеся поля,
промежуточное изменение с возвратом исходных полей и точное совпадение сохранённого
`search-*/screen.png` с входом Scanner. Это не подтверждает ответ сервера в игре.
После изменения Search обычный таймаут UNCHANGED продолжает live, но одноразовый
probe завершается NO_CHANGE. При нестабильности ожидание продлевается один раз;
UNSTABLE (в том числе переход обратно к исходному) пропускает OCR/BUY и допускает
новый Search в live. Тест проверяет переход срока между чтениями часов, не допуская
отрицательного sleep. Тесты нулевого polling продвигают fake время стоимостью захвата,
а не добавленным sleep. Полный захват после BUY не отменяется паузой/стопом:
завершение наблюдения результата сохраняется.

Тесты четырёхстрочного Search проверяют переход на девять строк к половине таймаута:
стабильное изменение снизу принимается без нового Search, нестабильное не поступает в OCR.
Подготовка Search (2026-10-20): test_search_refresh проверяет снятие hover до baseline,
пустую таблицу с поздней отрисовкой, стабилизацию нижних строк при trigger=4,
таймаут без клика, отмену и возможность обновить действительно пустой список.
Fake имена имеют яркий пиксель, чтобы отличать синтетический лот от пустой таблицы.
test_windows и test_background_desktop проверяют MOVE без DOWN, следующий клик
без повторного MOVE/ожидания, проверку фокуса/координат/отмены. Старые сценарии
post-Search отмены и переходных кадров привязаны к факту клика, а не номеру захвата:
подготовка теперь добавляет захваты до него. OCR и дополнительные проверки лота
не добавлялись; игровые задержки этим набором не подтверждаются.
Tk-тест проверяет видимость сообщения об ошибке под вкладками при высоте окна 760 px.

## Быстрые проверки

OCR баланса и автоматический бюджет: `tests/test_balance.py`, `tests/test_auto_balance.py`,
`tests/test_ui.py`. Проверяются временное списание/возврат, UNKNOWN, запись расхождения
без остановки, следующий Search/BUY с прочитанным балансом и допуск после перезапуска,
отказ OCR, однократная сверка, блокировка после паузы/стопа, старт до бюджетного
порога, продолжение и минутный интервал. Только fake desktop/время и временные БД.
Реальный OCR полной строки проверен на 36 кадрах, см.
[отчёт](../validation/balance-ocr-20261019/README.md). Полный цикл в игре не запускался.

```powershell
.\.venv-benchmark\Scripts\python.exe -m unittest discover -s tests -p test_bot.py -v
.\.venv-benchmark\Scripts\python.exe -m unittest discover -s tests -p test_validation.py -v
.\.venv-benchmark\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v
.\.venv-benchmark\Scripts\python.exe -m unittest discover -s tests -p test_hotkeys.py -v
.\.venv-benchmark\Scripts\python.exe -m unittest discover -s benchmarks -p "test_*.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s dataset -p test_builder.py -v
```

Полный комплект тестов бота — `-m unittest discover -s tests -v`; он включает Tk/clipboard.
При отсутствии desktop-сессии отметьте UI-проверку как невыполненную, не как успешную.
Ошибка запуска Python внутри sandbox сама по себе не доказывает поломку venv.

Известная регрессия на 2026-10-20: общая команда тестов benchmark завершается
ImportError (`lot_key` в `test_optimization_secondary`), хотя 39 других тестов проходят.
Этот прототип также содержит устаревшие ожидания схемы БД и UNKNOWN_RECOVERED;
не возвращайте старое поведение в runtime ради прохождения. Подробности и лог —
в [аудите, A04](AUDIT_2026-10-20.md). Тесты основного бота и полный CPU OCR-прогон
в рамках этого аудита прошли.

## Интеграционная регрессия

```powershell
.\.venv-benchmark\Scripts\python.exe tools\validate_bot.py --dataset dataset --models benchmarks\models --output validation\integration-new.json
```

Выберите новое имя отчёта: существующий файл и запись внутрь dataset запрещены.
Прогон сверяет хеши разметки/кропов/исходников, читает 170 размеченных строк,
сравнивает batch 1/3/9, цвет, числовые результаты и шесть UI-эталонов.
Дополнительно проверяет INSUFFICIENT на отдельном исходнике из
`validation/insufficient-20261015/source.png`, не меняя датасет и его baseline.
Перед каждым техническим NAME batch вызывается штатный warmup; время подготовки
отдельно сохранено в `warmup_ms`. Baseline и пороги покрытия остаются прежними.
17 Replay с искусственными ценами проверяют согласованность выбора между batch;
это не независимый эталон экономически правильной покупки.
Дополнительно для каждого кадра записываются фактические OCR-наблюдения девяти строк.
На их кэше проверяются 18 комбинаций группа × критерий × OCR batch. Независимый расчёт
по наблюдениям проверяет допустимость, прибыль/ROI, выбранную строку и границу группы;
он не является независимым эталоном правильности исходного OCR. Прежний group 1
сопоставляется с живыми OCR Replay. Разный group_size вправе выбирать разный лот;
одинаковая стратегия не должна зависеть от технического NAME batch.

`validation/baseline.json` фиксирует labels SHA-256, число строк и минимальное
числовое покрытие. Проверяются и правильные видимые числа, и совокупные результаты:
все отказы при `dangerous=0` не проходят. Точные пороги — в baseline, смысл метрик —
в [OCR](OCR.md). `--baseline PATH` позволяет явно выбрать другой проверенный baseline.
При новом датасете сначала обоснуйте критерии; не обновляйте baseline автоматически
из результатов изменяемого OCR и не снижайте пороги ради прохождения.

Новый отчёт содержит время UTC, baseline и хеши рабочего кода/выбранных весов.
`--ocr-device dml` проверяет рабочий GPU Reader (по умолчанию `cpu`), включая
batch 1/3/9 для проверки низкоуровневого API и частичных запросов кеша;
UI/CLI/Runner в режиме GPU используют эффективный batch 9. Запускайте CPU/GPU
серии последовательно и сохраняйте каждый результат под новым именем.
Результаты внедрения переключателя, native CPU → GPU → CPU и доказательство
DML-исполнения — [отчёт](../validation/gpu-switch-20261016.md).
Исторический `validation/integration.json` предшествует усиленной проверке покрытия;
после переноса используется `validation/integration-post-transfer-verified.json`.
JSON сохраняется до заключительных проверок: наличие файла не означает PASS.
Подтверждение успеха — код завершения 0 и сообщение `Integration regression passed`.

С внедрением NAME-кеша регрессия сохраняет исходный Reader без кеша для baseline
и отдельно сравнивает полный Identity с рабочей обёрткой на 170 кропах, batch 1/3/9,
при пустом кеше и повторе. На каждом Replay проверяются реальные Scanner с кешем
и без него для всех 18 сочетаний batch × группа × критерий, включая холодный/повторный
проход. Числа каждый раз читаются через native OCR. Поле `name_cache` содержит
результаты сравнения, счётчики и время NAME; это офлайн-повторы, не игровой hit rate.

## Эксперименты и данные

Сохраняемый NAME-кеш проверяют `tests/test_cache_storage.py`: точный порядок LRU,
лимит/0, совместимость, повреждения, атомарная запись, фоновые снимки и ошибки writer.
`tests/test_session_clock.py` и UI-тесты проверяют время сессии, паузу и завершение.
Для native-проверки перезапуска используйте новый каталог результатов:

```powershell
.\.venv-benchmark\Scripts\python.exe tools\validate_name_cache.py --output validation\persistent-cache-native-NEW
```

CPU и DML проверяются последовательно на 170 NAME-кропах, batch 1/3/9, с запретом
native NAME OCR после загрузки; числа распознаются заново. Дополнительно сравниваются
72 решения Scanner и CLI Replay с временной БД. Рабочие кеши не используются.
Результаты внедрения — [отчёт](../validation/persistent-cache-20261017.md).

Search сравнивается отдельно от OCR на одинаковом ограниченном наборе:

```powershell
.\.venv-benchmark\Scripts\python.exe tools\benchmark_search.py --capture --corpus-report validation\search-performance-20261015-165834-186764\report.json
```

`--capture` только измеряет захват текущего рабочего стола, не отправляет ввод
и не сохраняет скриншоты. Без этого флага эксперимент полностью файловый.
Каждый запуск создаёт новый каталог. Путь/хеш каждого кадра и версии окружения —
в report.json. Во время измерений не запускайте параллельно OCR-регрессию.
Модель polling/мыши не доказывает задержки игры; итог и план игровой проверки —
в [отчёте оптимизации](../validation/search-optimization-20261015.md).

Подбор моделей/preprocessing и возобновление длинных прогонов описаны в
[benchmarks/README.md](../benchmarks/README.md). Эксперимент не становится рабочим OCR
автоматически: перенос в `stalbot/vision.py` требует отдельной регрессии.
Схема разметки и редактирование кропов — в [Builder](../dataset/README_builder.md).
Новые независимые скриншоты держите отдельно от набора, на котором выбирали варианты.
Не редактируйте labels/crops одновременно с тестами или открытым Builder.
Изменения разметки, preprocessing и модели оценивайте раздельно, сохраняя происхождение результатов.

## Живой контур

Автоматические тесты не отправляют клики в игру. Перенос завершается CLI Replay
с отдельной БД и проверкой запуска UI; реальные покупки не служат smoke-тестом.
Офлайн-тесты не подтверждают SendInput, геометрию живого экрана, задержки Search
или уведомление нехватки средств. Пользователь подтвердил F8/F9 при активной игре.
Изменённые задержки мыши требуют повторной проверки меню без BUY. Статус проверок — в
[плане разработки](../DEVELOPMENT_PLAN.md).

## Почему документация устроена так

Один корневой AGENTS задаёт постоянные правила; ссылки раскрывают детали по задаче.
Вложенные AGENTS и repo-local skills пока не нужны: различающихся правил нет,
повторяемые проверки оформлены скриптами. Навык должен описывать самостоятельный
процесс, а не копировать README.
Основания, проверенные 2026-10-15: [иерархия AGENTS](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
[экономия контекста](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra),
[skills](https://learn.chatgpt.com/docs/build-skills),
[субагенты](https://learn.chatgpt.com/docs/agent-configuration/subagents).
