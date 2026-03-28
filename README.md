# Hello Again

Hello Again е проект за възрастни хора, които живеят сами, трудно използват модерни приложения и често са далеч от информация, услуги и човешки контакт, особено в малки и отдалечени населени места.

Идеята е проста: вместо човекът да се напасва към сложна технология, технологията да се напасва към човека чрез по-достъпен интерфейс, гласово управление и социално свързване.

## Какъв проблем решава

Проектът е насочен към:

- самота и социална изолация
- затруднено използване на дигитални услуги
- липса на достъпна и ясна комуникация
- нужда от по-естествен интерфейс за възрастни потребители

Hello Again комбинира гласови взаимодействия, по-опростен frontend и система за препоръчване на подходящи хора за общуване и приятелство.

## Връзка с Hack TUES 12

Проектът е в духа на **Hack TUES 12: Code to Care** и най-силно се връзва с темите:

- **Beyond the City**: фокус върху хора в малки и отдалечени населени места
- **LimitLess**: фокус върху достъпност и премахване на бариери при използване на технологии

## Основни възможности

- гласово onboarding изживяване
- voice-first навигация и взаимодействие
- friend discovery и изпращане на покани за приятелство
- meetup логика за срещи
- модул за препоръки на база описание, интереси, поведение и графови зависимости

## Технологии

- **Frontend**: Flutter
- **Backend**: Django
- **Mobile/native**: Kotlin
- **Database**: PostgreSQL / pgvector
- **Recommendation engine**: PyTorch + GAT/GNN

## Структура на проекта

```text
HelloAgain/
|- backend/
|  |- apps/
|  |- recommendations/
|  |- voice_gateway/
|  |- manage.py
|- frontend/
|  |- lib/
|  |- web/
|  |- android/
|  |- pubspec.yaml
|- mobile/
|- docker/
|- docker-compose.yml
|- .env
```

## Локално стартиране

### Предварителни изисквания

Нужно е да имаш инсталирани:

- Docker
- Python
- Flutter
- Chrome

### 1. Клониране / `git pull`

След като си дръпнал проекта, влез в root папката:

```powershell
cd HelloAgain
```

Точно там трябва да виждаш:

- `backend/`
- `frontend/`
- `docker-compose.yml`

### 2. Стартиране на базата данни

От root папката:

```powershell
docker compose up -d
```

### 3. Стартиране на backend-а

В нов терминал:

```powershell
cd backend
```

Ако нямаш виртуална среда:

```powershell
python -m venv venv
```

След това:

```powershell
.\venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Backend-ът по подразбиране тръгва на:

- `http://127.0.0.1:8000`
- `http://localhost:8000`

### 4. Стартиране на frontend-а

В трети терминал:

```powershell
cd frontend
flutter pub get
flutter run -d chrome
```

Това стартира Flutter web клиента в Chrome.

## `.env` файлове

Проектът използва няколко `.env` файла:

- `/.env` - общи стойности за root и Docker setup
- `backend/.env` - backend конфигурация
- `frontend/.env` - frontend конфигурация

Ако нещо не тръгва, провери първо тях.

## Как да разбереш, че проектът е пуснат успешно

- `docker compose up -d` минава без грешка
- `python manage.py runserver` стартира Django backend-а
- `flutter run -d chrome` отваря приложението в браузъра
- при web voice flow браузърът трябва да има разрешение за микрофон

## Recommendation engine

Препоръчващият модул е в `backend/recommendations/`.

Той:

- представя потребителите чрез набор от 64 характеристики
- моделира ги като възли в граф
- използва GAT/GNN логика за оценка на съвместимост
- комбинира графов сигнал с explainable compatibility логика

Целта не е просто да върне "подобен" човек, а по-подходящ човек за контакт, разговор и потенциално приятелство.

## Полезни проверки

### Backend

```powershell
cd backend
.\venv\Scripts\activate
pytest
python manage.py check
```

### Frontend

```powershell
cd frontend
flutter analyze
flutter test
```

## Накратко

Hello Again е достъпен социален и voice-first продукт, насочен към възрастни хора в по-изолирани общности. Целта му е да намали самотата, да улесни използването на технологии и да създава по-смислени човешки връзки.
