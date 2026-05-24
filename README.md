# MC Quest Generator

Автоматически генерирует цепочки квестов для **FTB Quests (Minecraft 1.12.2)** на основе ИИ.

ИИ изучает каждый мод (через Modrinth API + FTB Wiki), строит дерево прогрессии от базовых крафтов до эндгейма, и генерирует готовый файл `config/ftbquests/quests.json`, который сразу можно бросить в сборку.

## Что делает

- Один **глава (chapter)** в FTB Quests = один мод
- Квесты выстроены в цепочку: каждый зависит от предыдущего
- Поддержка нескольких модов за один запуск
- Язык квестов: английский или русский (`--lang ru`)
- **Наград нет** — это шпаргалка по прогрессии, не геймплейный хардкор

## Установка

```bash
git clone https://github.com/YOUR_GITHUB/mc-quest-gen
cd mc-quest-gen
pip install requests  # единственная зависимость
```

> Python 3.10+ required. Нет зависимостей кроме стандартной библиотеки + `requests` (опционально, используется urllib).

## Настройка API-ключей

```bash
python mc_quest_gen.py --setup
```

Поддерживаемые провайдеры (в порядке приоритета):

| Провайдер | Ключ | Бесплатно? |
|-----------|------|-----------|
| **OpenRouter** | `openrouter_api_key` | ✅ Free-tier модели |
| **Google Gemini** | `google_api_key` | ✅ Free quota |
| **Cloudflare Workers AI** | `cf_api_token` + `cf_account_id` | ✅ Бесплатный план |
| **G4F Groq** | `g4f_api_key` | ✅ |

Конфиг сохраняется в `providers_config.json` рядом со скриптом.

## Использование

```bash
# Тест провайдеров
python mc_quest_gen.py --test

# Генерация для одного мода
python mc_quest_gen.py -m "Thermal Expansion"

# Несколько модов
python mc_quest_gen.py -m "IC2" "Thermal Expansion" "Applied Energistics 2" "Ender IO"

# Из файла (один мод на строку)
python mc_quest_gen.py --mods-file my_mods.txt

# На русском языке
python mc_quest_gen.py -m "Draconic Evolution" --lang ru

# Своя папка вывода
python mc_quest_gen.py -m "Mekanism" -o ./my_modpack
```

### Файл модов (`mods.txt`)

```
# Комментарии начинаются с #
Thermal Expansion
IC2
Applied Energistics 2
Ender IO
Draconic Evolution
Botania
Tinkers Construct
Mekanism
```

## Выходные файлы

```
mc_quests_output/
└── config/
    └── ftbquests/
        └── quests.json   ← главный файл
```

Скопируй папку `config/ftbquests/` в папку с экземпляром Minecraft.

В игре: `/ftbquests editing_mode` чтобы открыть редактор.

## Структура квеста (пример)

```json
{
  "id": "3A4B5C6D",
  "title": "First Alloy: Electrical Steel",
  "text": [
    "Smelt Iron + Coal in the Alloy Smelter.",
    "Electrical Steel is required for almost every Ender IO machine."
  ],
  "tasks": [
    { "type": "item", "item": "enderio:item_alloy_ingot", "count": 1, "consume_items": false }
  ],
  "dependencies": ["1A2B3C4D"],
  "rewards": []
}
```

## Архитектура

```
mc_quest_gen.py   ← точка входа, CLI
providers.py      ← AI-провайдеры с автофолбеком (OpenRouter → Gemini → CF → G4F)
scraper.py        ← Modrinth API + FTB Wiki парсер
ftbquests.py      ← генератор FTB Quests JSON
providers_config.json  ← твои API ключи (создаётся при --setup)
```

## Примечания

- Квесты **не требуют сдачи предметов** (`consume_items: false`) — это только шпаргалка
- Если у AI не хватает информации о моде, он всё равно генерирует разумную прогрессию на основе названий предметов
- FTB Wiki может быть недоступна — в этом случае используются встроенные данные о ~10 популярных модах
- Для редких модов можно улучшить результат добавив описание в подсказку через `--verbose`

## Лицензия

MIT
