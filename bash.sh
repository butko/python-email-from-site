# Установка зависимостей (Astra Linux предпочтительно так)
sudo apt update
sudo apt install python3 python3-requests python3-bs4

# Запуск (файл config.ini должен лежать рядом или укажите путь)
python3 crawl_emails.py
# или:
python3 crawl_emails.py /путь/к/config.ini

# Запуск (вариант 2)
sudo /usr/bin/python3 crawl_emails.py
