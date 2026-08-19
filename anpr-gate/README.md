# ANPR Gate

İki Hikvision kamera ve iki kapı için production ANPR sistemi. Giriş ve çıkış
worker'ları aynı access list'i kullanır; yalnızca kapı tetikleme denemeleri
PostgreSQL audit kaydı olur. Otomatik olaylarda yeşil işaretli tam kare ve plaka
crop'u saklanır.

## Bileşenler

- `web`: FastAPI, Jinja ve HTMX yönetim paneli
- `worker-entry`, `worker-exit`: birbirinden bağımsız RTSP/ONNX worker'ları
- `postgres:16`: partition'lı event ve değiştirilemez admin audit metadata'sı
- `redis:7`: oturum, login rate limit, heartbeat, cache invalidation ve canlı kare pub/sub
- `archive-sync`: checksum doğrulamalı NFS aktarımı, 60 gün retention ve DB backup
- `cloudflared`: host portu açmadan public HTTPS

Compose içinde `cloudflared` public `edge` ağına, kamera/gate istemcileri ise LAN
çıkışı olan ayrı `lan` ağına bağlıdır. PostgreSQL ve Redis yalnızca `internal`
Docker ağında kalır; host üzerinde `80`, `443` veya `8000` portu publish edilmez.

## Kamera ayarı

NVR'nin H.265 ana kayıt akışını değiştirmeyin. İki kamerada da web arayüzünden
alt akışı şu şekilde ayarlayın:

- codec: H.264
- çözünürlük: 1280×720
- frame rate: 15 fps
- bitrate: CBR, yaklaşık 1536–2048 Kbps
- I-frame interval: 15
- RTSP channel: `Streaming/Channels/102`

Worker görüntüyü RTSP/TCP ile sürekli tüketir, yalnızca en yeni kareyi 1.5 fps
varsayılan hızla analiz eder. Önizleme 10 fps olarak bağımsız yayınlanır. ONNX
Runtime worker başına en fazla iki CPU inference thread'i kullanır.

## Ubuntu 24.04 VM kurulumu

Önerilen VM: 4 vCPU, 8 GB RAM ve güncel Ubuntu 24.04. Docker Engine/Compose
plugin, NFS client ve Tailscale kurulu olmalıdır.

NAS export'unu bağlayın (örnek):

```fstab
nas.example.lan:/anpr /mnt/anpr-archive nfs4 rw,_netdev,nofail,x-systemd.automount,noatime 0 0
```

Runtime dizinlerini oluşturup konteyner kullanıcısına verin:

```bash
sudo mkdir -p /srv/anpr/spool /srv/anpr/state/entry /srv/anpr/state/exit /mnt/anpr-archive
sudo chown -R 10001:10001 /srv/anpr /mnt/anpr-archive
```

Uygulama ayarını hazırlayın:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

`ENTRY_CAMERA_HOST` ve `EXIT_CAMERA_HOST` değerlerini LAN IP'leriyle değiştirin.
RTSP ve gate parolalarını `.env.production` içine yazmayın.

## Secret dosyaları

Şu dosyaları `secrets/` altında oluşturun:

- `postgres_admin_password`: yalnızca PostgreSQL bootstrap/restore kullanıcısı
- `database_password`: sınırlı `anpr_gate` uygulama rolü
- `entry_camera_password`, `exit_camera_password`
- `entry_gate_token`, `exit_gate_token`

Secret değerlerini shell history'ye yazmamak için dosyaları `sudoedit`/güvenli
secret yöneticisiyle doldurun. Gate driver `disabled` iken gate token dosyası boş
olabilir. Kamera veya gate parolaları DB'de ve uygulama loglarında tutulmaz.

```bash
sudo chown root:root secrets/*
sudo chmod 600 secrets/*
```

Bu nedenle production Compose komutlarını `sudo` ile veya root-owned bir systemd
unit'inden çalıştırın; secret dosyalarını Docker grubundaki kullanıcılara açmayın.

## Cloudflare Tunnel

Cloudflare'da tunnel ve public hostname oluşturun. Tunnel credentials JSON'unu
`docker/cloudflared/credentials.json` yoluna, örnek config'i de
`docker/cloudflared/config.yml` yoluna kopyalayın:

```bash
cp docker/cloudflared/config.yml.example docker/cloudflared/config.yml
chmod 644 docker/cloudflared/config.yml
sudo chown root:root docker/cloudflared/credentials.json
sudo chmod 600 docker/cloudflared/credentials.json
```

Config içindeki tunnel UUID ve `gate.example.com` alan adını değiştirin. Ingress
hedefi `http://web:8000` olarak kalmalıdır.

VM firewall'ında public inbound `22/80/443/8000` kapalı tutulmalı; SSH yalnızca
`tailscale0` üzerinden izinli olmalıdır. Proxmox host'un `8006` yönetim portu da
VM'den bağımsız olarak Proxmox firewall'da Tailscale-only yapılmalıdır.

## İlk çalıştırma

```bash
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d postgres redis
docker compose -f docker/docker-compose.yml run --rm web python -m app.cli init-db
docker compose -f docker/docker-compose.yml run --rm web python -m app.cli create-admin --username admin
docker compose -f docker/docker-compose.yml up -d
```

Admin parolası interaktif alınır, minimum 14 karakterdir ve Argon2id hash olarak
saklanır. Parola environment'a veya kaynak koda girmez.

Eski SQLite whitelist aktarımı idempotenttir:

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v "$PWD/data:/legacy:ro" web \
  python -m app.cli migrate-sqlite /legacy/anpr.db
```

## Gate entegrasyonu

İlk kurulum `ENTRY_GATE_DRIVER=disabled` ve `EXIT_GATE_DRIVER=disabled` gelir.
Gerçek röle HTTP endpoint'leri bilindiğinde her gate için:

```env
ENTRY_GATE_DRIVER=http
ENTRY_GATE_URL=https://relay.lan/api/gates/entry/open
EXIT_GATE_DRIVER=http
EXIT_GATE_URL=https://relay.lan/api/gates/exit/open
```

İstek Bearer token, JSON event bilgisi ve `Idempotency-Key: <event UUID>` ile
gönderilir. Uygulama otomatik retry yapmaz. Röle endpoint'inin idempotency key'i
desteklediği doğrulanmadan retry eklenmemelidir. İki gerçek röleyle ayrı ayrı
başarılı test yapılmadan production readiness verilmemelidir.

## Audit ve dayanıklılık

- Otomatik event sırası: access cache doğrulama → atomik snapshot → DB/outbox → gate → sonuç güncelleme.
- DB kesilirse her worker kendi WAL modlu SQLite outbox'ına yazar; UUID replay ile event tekilleştirilir.
- Son başarılı access-list cache'i yedi günden eskiyse gate fail-closed kalır.
- Access-list değişikliği Redis pub/sub ile iki worker'a hemen, ayrıca 30 saniyelik polling ile yeniden ulaşır.
- Ortak local spool 20 GB ile sınırlıdır. Dolarsa en eski aktarılmamış görseller silinir, metadata korunur ve event `evicted` olur.
- Archive worker her saat dosya boyutu ve SHA-256'yı doğrulayıp NFS'e taşır.
- Snapshot'lar 60 gün sonra silinir; event metadata'sı süresiz kalır.
- PostgreSQL günlük gzip backup'ları 30 gün, ayın ilk backup'ları 12 ay tutulur.

Son backup'ın gerçek restore testi:

```bash
docker compose -f docker/docker-compose.yml --profile maintenance run --rm restore-test
```

Bu komutu NAS üzerinde aylık timer/cron ile çalıştırın ve başarısızlığını alarm
sisteminize yönlendirin. Test geçici bir DB yaratır, şemayı doğrular ve DB'yi
çıkışta siler.

## Operasyon

```bash
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs --since=15m web worker-entry worker-exit archive-sync
docker stats
```

Teknik loglar JSON stdout'dur ve Compose tarafından rotate edilir. Dashboard:

- iki authenticated canlı kamera
- worker/camera/cache/outbox durumu
- DB, Redis, NAS sync ve son backup
- tarih, plaka, kamera, kaynak ve sonuç filtreli audit + CSV
- snapshot/crop detayları
- access list ekleme, düzenleme ve aktif/pasif yapma
- zorunlu gerekçe ve ikinci onayla manuel giriş/çıkış açma

Event ve admin audit kayıtlarını silen bir UI/API yoktur.

## Testler

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -v
```

PostgreSQL migration/replay testi yalnızca adı `_test` içeren ayrı bir DB ile çalışır:

```bash
TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1/anpr_gate_test \
  python -m unittest tests.test_postgres_integration -v
```

24 saat soak testinde worker restart/reconnect, ortalama CPU <%80, toplam RAM
<7 GB, trigger p95 <2 saniye ve kamera başına ≥10 fps önizleme ayrıca ölçülmelidir.

## Yerel MVP modu

Eski tek-kamera geliştirme uygulaması korunmuştur:

```bash
source .venv/bin/activate
cp .env.example .env
python -m app.main
```

Production kurulumu `app.web` ve iki ayrı `app.worker` kullanır; `app.main`
production entrypoint değildir.
