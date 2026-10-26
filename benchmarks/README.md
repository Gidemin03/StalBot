# StalBot OCR benchmark

Отдельный инструмент для оценки OCR по ручной разметке Dataset Builder. Не управляет
игрой, не покупает предметы и не изменяет исходники/кропы/разметку во время теста.
Исправления подтверждённых опечаток разметки выполнялись отдельно с резервной копией.

Актуальный выбор после второго этапа: [REPORT2.md](REPORT2.md).
`candidate_pipeline.py` проверяет выбранную CPU-схему на всём размеченном наборе:

```powershell
.\.venv-benchmark\Scripts\python.exe benchmarks\candidate_pipeline.py --dataset dataset --output benchmarks\results\candidate-new
```

Поле NAME очищается по расположению компонентов (`line3`), QTY проходит геометрическую
проверку наличия цифр и читается Tesseract после `white3`, BUYOUT — Tesseract `otsu3`.
При неизвестном QTY состояние остаётся AMBIGUOUS, а `effective_quantity=1` — только
нижняя оценка для расчёта стоимости единицы при корректной полной цене лота.
Дополнительный OCR цены включается только флагом `--price-fallback`.
Отдельные эксперименты: `field_experiments.py --dataset dataset --output NEW_DIR
--stage name|upgrade|quantity|detector|price` (нужно выбрать одно значение stage).
Сравнительный отчёт: `report2/TABLES.md`, просмотр ошибок: `report2/errors.html`.

Новые запуски поддерживают `--resume`: та же команда и папка с добавленным флагом.
Каждый ответ сохраняется отдельно; завершённые примеры пропускаются. Проверяются
код, разметка, настройки и модели. Несовместимый запуск не продолжается; старые
результаты без `checkpoint.json` также нельзя возобновлять этим способом.
После изменения кода или набора моделей используйте новую выходную папку.

## Оптимизация рабочего Reader (2026-10-16)

Новые `optimization_*.py` проверяют рабочую предобработку и неизменённые правила,
не перенося экспериментальные варианты в бот. Результаты и ограничения:
[OPTIMIZATION_20261016.md](OPTIMIZATION_20261016.md).
Выполняйте серии **последовательно**, из корня проекта, с новыми выходными каталогами:

```powershell
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_devices_isolated.py --output benchmarks\results\devices-new --blocks 3 --repeats 8 --frames 3
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_devices_inspect.py --output benchmarks\results\devices-new\effective-settings.json
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_cache.py --output benchmarks\results\cache-new --repeats 2 --scan --scan-repeats 2 --scan-batches 9 --scan-frame-limit 2 --device-spec dml_b9
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_pipeline.py --output benchmarks\results\pipeline-new --specs cpu_w2_t2 dml_b9 --request-sizes 1 9 --repeats 2 --frame-limit 2
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_combinations.py --output benchmarks\results\combinations-new --repeats 5
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_secondary.py --suite all --repeats 5 --output benchmarks\results\secondary-new
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_capture.py --repeats 60 --output benchmarks\results\capture-new
.\.venv-benchmark\Scripts\python.exe benchmarks\optimization_streams.py --output benchmarks\results\streams-new
```

У `optimization_*` нет `--resume`: сохраняйте каждый повтор в новый каталог.
Матрица устройств запускает каждую конфигурацию/раунд в отдельном процессе и
сохраняет stdout/stderr, в том числе native-сбои. Две/три независимые DML-сессии
на этой сборке приводили к access violation; для повторения только устойчивой части
добавьте `--skip-variants dml_sessions2 dml_sessions3`. Нулевая ошибка supervisor
означает успех устойчивой части; поле `all_candidates_pass` отдельно учитывает
сбои/исключения и не должно заменяться `stable_configurations_pass`.

`capture` читает текущий рабочий стол, не сохраняет пиксели и не отправляет ввод.
Остальные серии файловые; SQLite и запись диагностик используют временные данные.
`streams` измеряет только политику кеша на синтетических последовательностях,
без OCR. Corpus throughput, один пакет NAME, полный Scanner и модель polling —
разные интервалы. Повторы набора не оценивают игровой hit rate; FPS, свежесть
живого кадра, приём кликов и полный цикл покупки этими сериями не подтверждаются.

`--scan-frame-limit 2` / `--frame-limit 2` выбирает один обычный и один сложный
кадр; это ограниченная проверка Scanner. NAME сравнивается на всех 170 кропах.
Семантика ошибок, отмена/закрытие workers, коллизии/вытеснение кеша и независимость
RGB-копии проверяются общей командой `unittest discover -s benchmarks -p "test_*.py"`.

## Установка и запуск

Используется отдельное окружение в корне StalBot: `.venv-benchmark`. Окружение `.venv`
и Dataset Builder не затрагиваются. Из корня проекта:

```powershell
py -3.12 -m venv .venv-benchmark
.\.venv-benchmark\Scripts\python.exe -m pip install -r benchmarks\requirements-lock.txt
.\.venv-benchmark\Scripts\python.exe benchmarks\benchmark.py --dataset dataset --output benchmarks\results\new-run --engines rapid_cpu tesseract easy_cpu --variants raw scale3 otsu3 tight3
.\.venv-benchmark\Scripts\python.exe benchmarks\report.py --runs benchmarks\results\new-run --output benchmarks\report-new
```

Окружение и скачанные модели уже подготовлены на этом компьютере. При переносе нужны
файлы из `models`. RapidOCR/EasyOCR умеют скачать свои модели при первом запуске;
Tesseract требует `models/tessdata/rus.traineddata` и `eng.traineddata`.
Lock-файл рассчитан на Windows x64 и Python 3.12: wheel tesserocr привязан к этой версии.
Существующее окружение повторно создавать не требуется.
Фиксируются версии зависимостей, контрольные суммы датасета и моделей.
Первоначальное скачивание не входит в измерения скорости; загрузка и прогрев
моделей отдельно записаны в `*_runtime.json`.

Для GPU RapidOCR: `--engines rapid_dml`; для более ранней модели: `rapid_v4_cpu`.
`rapid_v4_cpu` — конфигурация PP-OCRv4 в RapidOCR; имя кириллического файла модели
может содержать PP-OCRv3, поэтому точную модель нужно смотреть в `model_hashes.json`.
CUDA EasyOCR доступна через адаптер `easy_cuda`, но требует CUDA-сборку PyTorch;
в проведённом CPU/DirectML-сравнении CUDA EasyOCR не тестировалась.

## Что сравнивается

- NAME, QTY и BUYOUT распознаются отдельно; детекция текста и классификатор поворота
  отключены, поскольку вход — уже известный горизонтальный ROI.
- `raw`: исходный RGB-кроп; собственная нормализация нейросети остаётся включённой.
- `scale3`: увеличение в 3 раза (bicubic).
- `otsu3`: увеличение ×3 и бинаризация Otsu.
- `tight3`: уменьшение ROI, затем ×3. BUYOUT: убрать 10 px слева, то есть X=1240;
  NAME: оставить верхние 16 px; QTY: строки 3..18 исходного кропа. Это эксперимент,
  исходная геометрия Builder не меняется.
- `white3`: выделить светлые малонасыщенные пиксели (min(R,G,B)>=175,
  max-min<=55), чёрные символы на белом фоне, увеличить ×3. Проверяется отдельно для QTY.
- `content3`: NAME/BUYOUT обрезаются по ограничивающему прямоугольнику всех
  пикселей с max(R,G,B)>100, с запасом 2 px, затем увеличиваются ×3. Это проверка
  пользы удаления пустого фона. Начальные компоненты, включая BID, не отбрасываются.
- EasyOCR/Tesseract используют ограничение алфавита цифрами для QTY.
  `easy_open_cpu` — контрольный вариант EasyOCR без этого ограничения.

На текущем наборе проводится **выбор кандидатов**, а не независимая финальная
оценка. Варианты выбираются по тем же 17 скриншотам, на которых посчитаны таблицы.
Будущие размеченные скриншоты нужно оставить для проверки зафиксированного выбора.
Точные повторения встречаются между соседними скриншотами, поэтому дополнительно
публикуется точность по уникальным кропам. Она не устраняет все корреляции данных.

## Метрики и безопасность интерпретации

Имя: точное совпадение, совпадение после NFKC и нормализации пробелов, CER,
совпадение видимой заточки. Нет fuzzy matching, исправления имени по каталогу,
замены букв цифрами, изменения регистра или смешивания +12/+15. Сокращённые имена
сравниваются с видимой подписью, а не с недоступным полным названием.

Числа: точное значение, состояние VALUE/ABSENT/AMBIGUOUS, завышение QTY,
занижение BUYOUT и выдача числа на AMBIGUOUS. Пустой результат QTY остаётся
AMBIGUOUS. В строгой метрике OCR значение 1 не подставляется. Геометрический фильтр
текущей схемы отклоняет иконки без подходящих цифр, но не доказывает ABSENT.
Отдельная расчётная метрика candidate_pipeline и бота использует нижнюю оценку 1;
её нельзя называть точностью OCR. Рабочая схема и знаменатели — в
[docs/OCR.md](../docs/OCR.md).

Числовой парсер принимает только целую строку с допустимым суффиксом валюты и
правильными группами по три цифры, если есть пробелы. Он не вырезает произвольные
буквы и не склеивает BID с BUYOUT. Ответы `4 9 рублей` и `1 100 00 рублей`
отклоняются. `---` для BUYOUT означает ABSENT. Высокая confidence не доказывает
корректность — в отчёте есть таблицы покрытия/ошибок для нескольких порогов.

`false_price_gate_5m` и `false_price_gate_20m` — диагностические пересечения
фиксированного лимита цены; это **не фактические ложные BUY**. Для последних нужны
каталог, разрешённые варианты предметов и торговые правила. Цвет имени хранится
в эталоне, но этот текстовый benchmark не оценивает распознавание редкости.

Время p50/p95 включает подготовку массива, inference и парсинг, не включает
чтение файлов, инициализацию, прогрев и сравнение с эталоном. Основная матрица
содержит один замер на пример. `profile_runtime.py` выполняет повторные замеры
нативных батчей 1/3/9 на двух выбранных скриншотах; показывает время батча и одного
изображения, а также различия ответов при изменении batch size. Выдача всех 9 лотов
одним пакетом меняет задержку первого решения — throughput не заменяет эту метрику.

## Артефакты

- `REPORT.md`: выводы и выбор кандидатов на этом компьютере.
- `candidates.json`: зафиксированные варианты для следующей независимой проверки.
- `results/*/manifest.json`: снимок разметки, версии пакетов и настройки запуска.
- `predictions.jsonl`: каждый сырой ответ, confidence, ожидаемые значения и время.
- `summary.json`: метрики отдельно ordinary/hard/all.
- `report/TABLES.md`, `summary.csv`: сравнительные таблицы.
- `report/errors.html`: автономный поиск и просмотр ошибок с изображениями.
- `report/confidence_curves.csv`: влияние порога confidence, без калибровки на тесте.
- `profile*/batch_timings.json`: повторные измерения CPU/GPU и пакетной обработки.
- `profile*/gpu_proof.json`: реально выполнявшиеся DirectML-узлы, не только наличие провайдера.
- `profile*/model_hashes.json`: точные файлы моделей и SHA-256.

Пересчёт после исправления эталона: `regrade.py --dataset dataset --input OLD_RUN
--output NEW_RUN`. Сохраняются ответы и времена, проверяется неизменность каждого
кропа; старый manifest и список изменений остаются в новом отчёте. При изменении
кропа требуется повторное распознавание. Папки запусков не перезаписываются.

Тесты из корня: `.\.venv-benchmark\Scripts\python.exe -m unittest discover -s benchmarks -p "test_*.py" -v`.

## Первичные источники

- RapidOCR API и модели: https://rapidai.github.io/RapidOCRDocs/main/en/install_usage/rapidocr/usage/
- RapidOCR DirectML: https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/how_to_use_infer_engine/
- PaddleOCR кириллические модели: https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_recognition.en.md
- EasyOCR API: https://www.jaided.ai/easyocr/documentation/
- ONNX Runtime DirectML: https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html
- tesserocr Windows: https://github.com/simonflueckiger/tesserocr-windows_build
- Модели Tesseract: https://github.com/tesseract-ocr/tessdata_fast
