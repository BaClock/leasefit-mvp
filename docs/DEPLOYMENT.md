# Развёртывание и эксплуатация

Эта инструкция подготовлена; внешнее развёртывание не выполнялось. Рекомендуемый пилот: один сервер Linux, Docker Compose, PostgreSQL на закрытой сети Compose, Nginx на хосте, TLS-сертификат доверенного центра. Веб-порт привязан к `127.0.0.1`, БД наружу не публикуется. Миграции выполняются перед запуском Gunicorn; сервер — один экземпляр приложения с двумя worker-процессами.

## HTTPS

1. Подготовьте отдельный домен кабинета (например, `app.example.org`), сервер и DNS. Установите Docker/Compose, Nginx и Certbot из официальных источников. Не копируйте учебную базу в реальный пилот.
2. Склонируйте репозиторий, создайте `.env` из примера со случайными ключом Django и паролем БД. `chmod 600 .env`. В рабочем окружении:

   ```dotenv
   DEBUG=0
   ALLOWED_HOSTS=app.example.org,localhost,127.0.0.1
   CSRF_TRUSTED_ORIGINS=https://app.example.org
   SECURE_SSL_REDIRECT=1
   SECURE_HSTS_SECONDS=31536000
   WEB_PORT=8000
   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
   EMAIL_HOST=smtp.your-provider.example
   EMAIL_PORT=587
   EMAIL_USE_TLS=1
   EMAIL_HOST_USER=your-smtp-user
   EMAIL_HOST_PASSWORD=replace-with-real-smtp-secret
   DEFAULT_FROM_EMAIL=LeaseFIT <noreply@app.example.org>
   SUPPORT_EMAIL=support@app.example.org
   ```

   Не публикуйте `.env` и резервные копии. При необходимости храните секреты во внешнем менеджере и передавайте их окружению контейнера. Если БД уже создана, смена `POSTGRES_PASSWORD` в `.env` сама по себе не меняет пароль существующего PostgreSQL-пользователя — выполняйте согласованную ротацию в БД.
3. Получите сертификат. Сначала настройте только HTTP-сервер с `/.well-known/acme-challenge/`, создайте `/var/www/certbot`, затем, например: `sudo certbot certonly --webroot -w /var/www/certbot -d app.example.org`. Используйте фактический домен и рабочий email оператора. Только после выдачи сертификата включите HTTPS-блок из `docs/nginx.conf.example`.
4. Замените домен и пути сертификата в примере Nginx, выполните `sudo nginx -t`, затем `sudo systemctl reload nginx`. Включите таймер автоматического продления и deploy hook перезагрузки Nginx. Проверьте `sudo certbot renew --dry-run`.
5. Запустите `docker compose up -d --build`. Проверьте `/healthz`, HTTPS `/login`, редирект HTTP→HTTPS, secure/HttpOnly session cookie и отсутствие доступа к порту приложения извне. Заголовок `X-Forwarded-Proto` доверяется только этому proxy; не публикуйте порт 8000 на всех интерфейсах.
6. Выполните `docker compose exec web python manage.py check --deploy`. Под текущим HTTPS окружением не должно быть предупреждений о DEBUG, cookie, ключе или перенаправлении. Допустимо W021: preload HSTS намеренно не включён, поскольку домен ещё не выбран и не подаётся в браузерный preload list. Проверьте почтовое восстановление, rate limit входа/заявки (429) и ограничения размера файлов. Значение HSTS включайте только после проверки HTTPS; оно применяется ко всем поддоменам этого имени.
7. Создайте агентство и первого руководителя, пароль вводится интерактивно:

   ```bash
   docker compose exec web python manage.py create_agency --name 'Название агентства' --email leader@agency.example
   ```

   Для оператора заявок при необходимости: `docker compose exec web python manage.py createsuperuser`. Продуктовые запросы/отчёты не редактируются через admin. Не выдавайте staff/superuser риелторам. Руководитель создаёт приглашения в `/app/team`, передаёт ссылки лично; срок — 7 дней.
8. Утвердите условия `/privacy`, контакт поддержки и правила хранения **до ввода реальных контактов/документов**. Кнопки отдельного лендинга затем могут вести на `https://<домен-кабинета>/login` и `/pilot`; изменение лендинга выполняется отдельной задачей.

Проверка корпоративного TLS-прокси при сборке: Dockerfile поддерживает необязательный BuildKit secret `proxy_ca`. В локальном override добавьте `build.secrets: [proxy_ca]`, где `secrets.proxy_ca.file` — доверенный CA bundle вашей среды. Сертификат используется только в шаге установки зависимостей, не копируется в образ; TLS-проверка не отключается. На обычном сервере override не нужен.

## Резервное копирование

PDF и исходники хранятся в той же БД, поэтому согласованного `pg_dump` достаточно для всех продуктовых данных. Секреты `.env`, код/версию образа и конфигурацию proxy храните отдельно в защищённом месте.

```bash
cd /path/to/leasefit-mvp
sh scripts/backup.sh /secure/backup/leasefit
```

Скрипт создаёт PostgreSQL custom-format dump и SHA-256 с правами текущего пользователя и `umask 077`. Пишет во временный файл, публикует его только после успешного `pg_dump`. Планировщик может запускать ежедневно. Рабочая цель пилота: RPO до 24 часов, RTO проверяется восстановлением. Рекомендуемая исходная ротация: 7 ежедневных / 4 недельных / 3 месячных; согласуйте с агентством. Копируйте зашифрованные резервные копии на отдельное хранилище, ограничьте доступ и проверяйте место. Один именованный том не является резервной копией.

## Учебное восстановление без перезаписи рабочей БД

```bash
sha256sum -c /secure/backup/leasefit/leasefit-TIMESTAMP.dump.sha256
sh scripts/restore.sh /secure/backup/leasefit/leasefit-TIMESTAMP.dump restore_check_20261116
```

Скрипт создаёт **новую** БД и отказывается работать с уже существующей; не делает `DROP`, `--clean` или удаления рабочей базы. Для проверки:

```bash
docker compose exec -e POSTGRES_DB=restore_check_20261116 web python manage.py check
docker compose exec -e POSTGRES_DB=restore_check_20261116 web python manage.py shell --no-imports -c "from leasefit.models import ClientRequest,Report,Attachment; print(ClientRequest.objects.count(), Report.objects.count(), Attachment.objects.count())"
```

Сверьте число строк, SHA-256 сохранённых PDF/вложений, журнал миграций и выборочные скачивания через отдельный изолированный экземпляр приложения. Регулярно выполняйте такую проверку. При реальном восстановлении сначала остановите запись, сохраните повреждённую БД для разбора, проверьте новую базу и только затем переключайте `POSTGRES_DB`/пересоздавайте web по согласованному регламенту. Не подключайте второй экземпляр с записью к проверочной базе одновременно с производственным переключением.

## Обновления

Перед обновлением: резервная копия, фиксация commit SHA/образа, тесты на отдельной базе. Затем `git pull --ff-only` до выбранного релиза и `docker compose up -d --build`. Миграции применяются при старте. Проверяйте health, вход, пересчёт и скачивание старого PDF. Автоматического отката данных нет: обратно совместимые миграции предпочтительны; восстановление из копии — отдельный контролируемый процесс. Контролируйте ошибки, размер БД, свободное место и возраст последней успешной копии. Содержимое отчётов/исходников и invitation/reset tokens не пишутся в access log приложения.
