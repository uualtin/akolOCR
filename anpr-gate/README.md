# ANPR Gate MVP

Ubuntu üzerinde Docker ile çalışan sade plaka tanıma ve gate tetikleme uygulaması.
İki RTSP kamera hattı, ortak SQLite access list, audit log ve tek web panelinden
oluşur. İlk testte giriş ve çıkış için aynı kamera URL'si kullanılabilir.

## Davranış

- Giriş ve çıkış RTSP akışları bağımsız okunur.
- ONNX plaka tanıma modeli iki kamera tarafından sırayla paylaşılır.
- Confidence ve confirmation eşiğini geçen plaka access listteyse audit loguna
  plaka ve sunucu zamanı yazılır.
- Aynı plaka kamera başına cooldown süresi boyunca yeniden tetiklenmez.
- `console` modunda gate OPEN işlemi yalnızca container loguna yazılır.
- `http` modunda ilgili gate URL'sine `{"plate":"34ABC123"}` POST edilir.
  Gate'in kapanmasını kendi kontrolörü yönetir; uygulama CLOSE göndermez.
- Görüntü veya screenshot diske kaydedilmez.

## Ubuntu kurulumu

Projeyi klonlayıp uygulama dizinine girin:

```bash
git clone https://github.com/uualtin/akolOCR.git
cd akolOCR/anpr-gate
```

Ayar dosyasını oluşturun:

```bash
cp .env.example .env
nano .env
```

İlk tek-kamera testinde iki URL aynı kalabilir:

```env
ENTRY_RTSP_URL=rtsp://admin:PAROLA@192.168.254.115:554/Streaming/Channels/102
EXIT_RTSP_URL=rtsp://admin:PAROLA@192.168.254.115:554/Streaming/Channels/102
GATE_TRIGGER_TYPE=console
```

RTSP parolasında `@`, `:`, `/`, `#` gibi URL karakterleri varsa parola kısmını
percent-encode edin. Gerçek röleler doğrulanana ve giriş/çıkış kameraları ayrılana
kadar `GATE_TRIGGER_TYPE=console` değerini değiştirmeyin.

Image'ı oluşturup çalıştırın:

```bash
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs -f anpr-gate
```

Paneli açın:

```text
http://VM_IP_ADRESI:8000
```

Panelden access liste plaka ekleyin. Kamera bu plakayı doğruladığında audit
tablosunda görünür ve logda iki kamera için ayrı OPEN satırları oluşabilir:

```text
[GATE_TRIGGER] gate=entry action=OPEN plate=34ABC123
[GATE_TRIGGER] gate=exit action=OPEN plate=34ABC123
```

## Gerçek gate OPEN çağrısı

İki kamera ve iki gate ayrı ayrı doğrulandıktan sonra `.env` dosyasını değiştirin:

```env
GATE_TRIGGER_TYPE=http
ENTRY_GATE_OPEN_URL=http://entry-relay.lan/open
EXIT_GATE_OPEN_URL=http://exit-relay.lan/open
```

Yeni ayarı uygulayın:

```bash
sudo docker compose up -d --force-recreate
```

## Veri ve bakım

Access list ve audit log yalnızca `data/anpr.db` dosyasındadır. Yedek için
container'ı durdurup bu dosyayı kopyalamak yeterlidir:

```bash
sudo docker compose stop
cp data/anpr.db data/anpr-backup.db
sudo docker compose start
```

Güncelleme:

```bash
git pull
sudo docker compose up -d --build
```

Testler:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -v
```
