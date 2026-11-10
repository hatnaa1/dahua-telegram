# Dahua → Telegram гүүр

Dahua камераас SMTP имэйлээр ирсэн хөдөлгөөний дохио, JPEG/PNG зургийг Telegram сувагт дамжуулах Python 3.12+ үйлчилгээ. `app.py` нь SMTP болон нэмэлт HTTP webhook хүлээн авна. `configure_dahua.py` нь LAN дотроос камерын Email, MotionDetect тохиргоог HTTP Digest API-аар уншиж, өөрчилнө.

## 1. Архитектур

```text
Dahua 192.168.1.50
  SMTP → 192.168.1.245:25 (MikroTik LAN хаяг)
    dst-nat → 119.40.100.100:2525 (topnet сервер)
      Docker host :2525 → dahua-telegram container :2525
        HTTPS :443 → Telegram Bot API → суваг -1003974950304
```

`topnet` дээр ажиллаж буй `akuvox-telegram` контейнертэй нэг Docker host-ын нөөцийг хуваалцана. Доорх Compose нь `dahua-telegram` нэртэй **тусдаа контейнер** үүсгэнэ. Akuvox контейнерийг солих, зогсоох, түүний `.env`-ийг дарж бичих шаардлагагүй. SMTP/HTTP host портууд давхцаж болохгүй. Хэрэв Akuvox аль хэдийн host-ын `2525` портыг эзэлсэн бол доорх портын сонголтын зааврыг дагана.

| Бүрэлдэхүүн | Хаяг / порт | Үүрэг |
|---|---|---|
| Dahua | `192.168.1.50` | Motion → snapshot → email |
| MikroTik | `192.168.1.245:25/TCP` | Камерын SMTP холболтын destination NAT |
| topnet | `119.40.100.100:2525/TCP` | Docker publish хийсэн SMTP порт |
| dahua-telegram | `2525/TCP`, `8090/TCP` | SMTP, HTTP `/health`, `/event` |
| Telegram | `api.telegram.org:443` | Bot API-аар сувагт нийтлэх |

Камерын gateway нь MikroTik рүү хүрэх зөв маршруттай байна. MikroTik-ийн WAN-аас topnet хүртэл TCP 2525 нээлттэй, буцах чиглэлийн маршрут/NAT зөв байх ёстой. Энэ нь LAN → интернет рүү чиглэсэн dst-nat; WAN-аас камер руу порт нээх заавар биш.

## 2. MikroTik тохиргоо

Эхлээд Winbox Safe Mode ашиглан одоогийн дүрмээ шалгаж, давхар дүрэм үүсгэхээс зайлсхий. `WAN` interface list нь бодит uplink-ийг агуулсан эсэхийг шалга. NAT болон filter дүрмийн дараалал чухал: доорх зөвшөөрөх дүрмийг тухайн урсгалыг хаадаг `forward drop`-оос өмнө байрлуулна; established/related зөвшөөрөх дүрэм хэвээр хэрэгтэй.

### Terminal

```routeros
/ip firewall nat print detail
/ip firewall filter print detail
/interface list member print

/ip firewall nat add chain=dstnat src-address=192.168.1.50 dst-address=192.168.1.245 protocol=tcp dst-port=25 action=dst-nat to-addresses=119.40.100.100 to-ports=2525 comment="Dahua SMTP to topnet"

/ip firewall filter add chain=forward src-address=192.168.1.50 dst-address=119.40.100.100 protocol=tcp dst-port=2525 connection-nat-state=dstnat action=accept comment="Allow Dahua SMTP to topnet"
```

Дээрх `filter add` нь дүрмийг төгсгөлд нэмнэ. Winbox эсвэл `move` командаар хаах дүрмийн өмнө зөөнө; дүрмийн дугаарыг өөрийн `print` үр дүнгээс авна. Filter дээр NAT-ын **дараах** `119.40.100.100:2525` хаяг/портыг хэрэглэнэ. `input accept :25` нэмэх шаардлагагүй: dst-nat хийсэн холболт `forward` chain-аар явна.

Одоо байгаа WAN masquerade/src-nat нь `192.168.1.50`-ийн энэ урсгалыг хамардаг бол дахин нэмэхгүй. Хамрахгүй, мөн topnet дээр LAN руу буцах маршрут байхгүй бол нарийвчилсан дүрэм нэмнэ:

```routeros
/ip firewall nat add chain=srcnat src-address=192.168.1.50 dst-address=119.40.100.100 protocol=tcp dst-port=2525 out-interface-list=WAN action=masquerade comment="Dahua SMTP WAN source NAT"
```

Нэмэлт камер бүрт өөрийн `src-address`-тай dst-nat/filter дүрэм нэмэх эсвэл зөвшөөрсөн камерын address-list хэрэглэнэ. Бүх LAN төхөөрөмжид шаардлагагүйгээр нээхгүй. NAT өөрчилсний дараа шинэ SMTP холболтоор шалга; connection tracking-д хуучин хөрвүүлэлт хадгалагдаж болно.

### Winbox GUI

| Алхам | Цэс / таб | Утга |
|---|---|---|
| 1 | IP → Firewall → NAT → `+` → General | Chain=`dstnat`, Src. Address=`192.168.1.50`, Dst. Address=`192.168.1.245` |
| 2 | General | Protocol=`6 (tcp)`, Dst. Port=`25` |
| 3 | Action | Action=`dst-nat`, To Addresses=`119.40.100.100`, To Ports=`2525` |
| 4 | Comment → OK | `Dahua SMTP to topnet`; давхцах дүрмийн өмнө байрлуул |
| 5 | Filter Rules → `+` → General | Chain=`forward`, Src. Address=`192.168.1.50`, Dst. Address=`119.40.100.100`, Protocol=`tcp`, Dst. Port=`2525` |
| 6 | Advanced / Connection NAT State | `dstnat` (RouterOS/Winbox хувилбараас табын байрлал өөр байж болно) |
| 7 | Action → OK | `accept`; холбогдох `forward drop`-оос өмнө зөө |
| 8 | NAT → одоогийн `srcnat` дүрэм | WAN masquerade энэ урсгалыг хамарч байгааг шалга; шаардлагатай үед дээрх нарийвчилсан srcnat-ыг GUI-аар үүсгэ |

Албан тайлбар: [MikroTik Port forwarding](https://help.mikrotik.com/docs/spaces/RKB/pages/154042388/Port%2Bforwarding), [NAT](https://help.mikrotik.com/docs/spaces/ROS/pages/3211299/NAT), [Filter](https://manual.mikrotik.com/docs/cli-reference/ip/firewall/filter/).

## 3. Dahua Web UI

LAN-аас `http://192.168.1.50` нээж админ эрхээр нэвтэрнэ. Firmware-аас хамаарч цэс, талбарын нэр өөр байж болно; доорх нь Email/Snapshot/Motion тохиргоонд тааруулах утгын заавар. Алхам бүрийн дараа **Save / Apply** дарна.

### SMTP Email

Ердийн зам: **Setting → Network → SMTP (Email)** эсвэл **Network → Email**.

| Алхам | Талбар | Тохируулах утга / тайлбар |
|---|---|---|
| 1 | Enable | Идэвхжүүл |
| 2 | SMTP Server | `192.168.1.245` — topnet хаягийг биш, MikroTik-ийн LAN хаягийг оруул |
| 3 | Port | `25` — MikroTik үүнийг topnet `2525` руу хөрвүүлнэ |
| 4 | Encryption / SSL / TLS | `None` / унтраалттай; энэ SMTP listener TLS ашиглахгүй |
| 5 | Anonymous | Унтраа (`Anonymous=false`); firmware 2.800 anonymous SMTP илгээхгүй |
| 6 | Username / Password | `Authentication=true`; хоосон бус username/password хэрэглэ (`dahua` / `dahua123`); bridge authentication идэвхтэй бол SMTP_USER/SMTP_PASS-тай тааруул |
| 7 | Sender | `dahua-front@camera.local`; камер бүрт ялгаатай болго |
| 8 | Receiver | `alerts@camera.local`; энэ нь bridge-ийн хүлээн авах шошго, бодит mailbox шаардлагагүй |
| 9 | Subject / Title | `[Front Door] Motion` гэх мэт камерын тогтвортой нэртэй гарчиг |
| 10 | Attachment | Асаа; зурагтай имэйл дамжуулахад шаардлагатай |
| 11 | Interval / Health mail | Хэт ойр давтахгүй утга сонго; шаардлагагүй тогтмол health email-ийг унтраа |
| 12 | Save → Email Test | Telegram-д туршилтын текст эсвэл зураг ирснийг шалга |

SMTP AUTH ашигласан ч камер → сервер хэсэг TLS-гүй тул нууц үг, зураг шифрлэгдэхгүй. Нийтийн сүлжээгээр дамжуулахдаа VPN/private route ашиглах нь тохиромжтой; topnet-ийн 2525 портыг камерын сүлжээний бодит WAN IP-д хязгаарла. Энэ bridge SMTP хүлээн авагч болохоос гадны mailbox руу relay хийхгүй.

### Snapshot

Ердийн зам: **Setting → Camera → Video → Snapshot**; зарим firmware-д **Storage → Schedule → Snapshot** тусдаа байна.

| Алхам | Талбар | Үйлдэл |
|---|---|---|
| 1 | Channel | Камерын үндсэн суваг / Channel 1 |
| 2 | Snapshot Type / Mode | `Event` / `Trigger`; зөвхөн `General` тогтмол зураг сонгохгүй |
| 3 | Image Size / Quality | Камерын дэмждэг JPEG нягтрал, уншигдах чанар сонго |
| 4 | Interval / Snapshot count | Эхлээд 1 зураг, тохиромжтой интервал; дараа нь хэрэгцээнд тохируул |
| 5 | Snapshot Schedule (байвал) | Motion/Event цагийн хуваарийг турших үед бүх өдөр, 00:00–24:00 болго |
| 6 | Save | Дараа нь Motion Detection-ийн Snapshot linkage-ийг мөн асаа |

### Motion Detection

Ердийн зам: **Setting → Event → Video Detection → Motion Detection**.

| Алхам | Талбар | Үйлдэл |
|---|---|---|
| 1 | Enable / Channel 1 | Асаа |
| 2 | Period / Working Period | Туршихдаа өдөр бүр 00:00–24:00; дараа нь бодит цагийн хуваарь |
| 3 | Region / Area → Setup | Хөдөлгөөн хянах хэсгийг зурж хадгал |
| 4 | Sensitivity / Threshold | Орчны гэрэл, мод, замын хөдөлгөөнд тохируул; CLI Level нь 1–6 хүрээтэй, UI шкал firmware-аас хамаарна |
| 5 | Anti-dither / Latch | Давталт багасгах тохиромжтой хугацаа сонго |
| 6 | Send Email | Асаа |
| 7 | Snapshot | Асаа; шаардлагатай бол Channel 1-ийг сонго |
| 8 | Save → бодит хөдөлгөөн | Telegram-д зураг, камерын нэр, үйл явдлын гарчиг ирснийг шалга |

Email Test амжилттай болсон нь Motion бүс, хуваарь, Snapshot linkage зөв гэсэн баталгаа биш. Бодит хөдөлгөөнөөр тусад нь шалгана.

### configure_dahua.py CLI

**Critical firmware requirement — DH-IPC-HDBW1230E-S5, firmware 2.800:** the camera refuses to send SMTP mail with `Anonymous=true` or an empty username/password, even when aiosmtpd accepts any credentials. Always use `Anonymous=false`, `Authentication=true`, and non-empty credentials. The CLI uses `DAHUA_SMTP_USER` / `DAHUA_SMTP_PASS` overrides, then `SMTP_USER` / `SMTP_PASS`; when `SMTP_USER` is empty, missing credentials default to `dahua` / `dahua123`. A configured SMTP_USER requires a password. If the bridge enforces authentication, the camera credentials must match it.

An initially empty `Email.Receivers` list cannot be initialized through CGI on this firmware. The CLI first reads the Email table through RPC2 and initializes the receiver with `configManager.setConfig`, preserving the other Email fields, then applies and verifies settings through CGI. RPC2 uses MD5 challenge login on `/RPC2_Login` and methods on `/RPC2`. `--test-mail` calls `netApp.sendTestMail` immediately using the camera’s saved settings; it does not configure the camera. An accepted RPC request is not proof of Telegram delivery.

Камерын LAN-д хүрэх Windows/Linux машинаас ажиллуулна. Docker image нь зөвхөн `app.py`-г агуулдаг тул CLI-г host/ажлын компьютер дээр ажиллуулна.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# .env дотор DAHUA_PASSWORD болон бусад утгыг бөглөнө
.\.venv\Scripts\python.exe configure_dahua.py --show
.\.venv\Scripts\python.exe configure_dahua.py --configure
.\.venv\Scripts\python.exe configure_dahua.py --apply --motion-level 3
.\.venv\Scripts\python.exe configure_dahua.py --test-mail
.\.venv\Scripts\python.exe configure_dahua.py --disable
```

Linux дээр `python3 -m venv .venv`, `.venv/bin/python` хэрэглэнэ. Өөр файл сонгох жишээ: `python configure_dahua.py --env-file camera-front.env --show`.

| Сонголт | Үйлдэл |
|---|---|
| `--show` | Email болон MotionDetect[0]-ийн тохиргоог уншиж нууц үгийн утгыг `********` болгоно |
| `--configure`, `--apply` | Ижил үйлдэл: SMTP, attachment, motion, email/snapshot linkage-ийг асаана; дэмжигдсэн TLS/auth талбарыг тохируулна |
| `--motion-level 1..6` | DAHUA_MOTION_LEVEL-ийг тухайн дуудлагад орлоно |
| `--test-mail` | RPC2 `netApp.sendTestMail` ашиглан камераас шууд туршилтын имэйл илгээнэ; bridge log болон Telegram-д хүрснийг шалга |
| `--disable` | MotionDetect[0].Enable болон email linkage-ийг унтраана; SMTP үйлчилгээг бүхэлд нь унтраахгүй |
| `--env-file PATH` | Өгөгдмөл нь скрипттэй зэрэгцэх `.env`; процессын environment утга файлаас давуу эрхтэй |

Нэг дуудлагад `--show`, `--configure`/`--apply`, `--test-mail`, `--disable`-аас нэгийг сонгоно. `DAHUA_PASSWORD` хоосон үед `DAHUA_PASS` нөөц утга болно. CLI нь шаардлагатай талбарууд firmware-д байгаа эсэхийг бичихээс **өмнө** шалгана. Талбар дутвал өөрчлөлт илгээхгүй, Web UI ашиглахыг заана. Бичсэний дараа password-оос бусад утгыг дахин уншиж харьцуулна; mismatch гарвал хэсэгчилсэн өөрчлөлт үлдсэн байж болно. CLI нь snapshot quality, region, schedule-ийг тохируулахгүй: Web UI-аар заавал шалгана. Камерын HTTP API нь LAN-д зориулагдсан, интернет рүү нээхгүй.

## 4. topnet Docker үйлчилгээ

Энэ хэсгийн команд нь topnet-ийн Linux shell-д зориулагдсан. Төслийг тусдаа `/opt/dahua-telegram` хавтаст хуулж, тэр хавтаснаас ажиллуулна.

```bash
cd /opt/dahua-telegram
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
docker port akuvox-telegram
ss -lnt | grep -E ':(2525|8090)\b'
cp .env.example .env
chmod 600 .env
# editor ашиглан .env утгуудыг бөглөнө
```

| .env хувьсагч | Утга / тайлбар |
|---|---|
| TELEGRAM_TOKEN | Сувагт нийтлэх эрхтэй bot-ын token; жишээ нууц утгыг repository-д оруулахгүй |
| TELEGRAM_CHAT_ID | `-1003974950304` |
| DEVICE_NAME | Identity олдохгүй үед ашиглах камерын нэр |
| SMTP_USER / SMTP_PASS | Өгөгдмөл `dahua` / `dahua123`; bridge authentication идэвхтэй бол камерын тохиргоотой ижил |
| COOLDOWN_S | Өгөгдмөл `15`; камер ба event тус бүрийн давталт дарах секунд; `0` бол дарахгүй |
| SMTP_BIND / SMTP_PORT | `0.0.0.0` / `2525`; host-ын SMTP publish хаяг/порт |
| HTTP_BIND / HTTP_PORT | Зөвхөн local шалгалтад `127.0.0.1` / `8090` болгохыг зөвлөе |
| WEBHOOK_TOKEN | `/event` ашиглавал урт санамсаргүй нууц утга; хоосон бол HTTP event нэвтрэлт шаардахгүй |
| DAHUA_* | CLI-ийн камер, SMTP server/port, Sender, Receiver, Title, motion level утгууд; bridge runtime эдгээрийг хэрэглэхгүй |

Bot-ыг Telegram сувагт админаар нэмээд **Post Messages** эрх өгнө. Нэг token-ыг Akuvox болон Dahua bridge-д ашиглаж болно: энэ код Telegram polling/webhook бүртгэхгүй, зөвхөн илгээх API ашиглана. Bot-ын эрх болон Telegram rate limit-ийг хоёр үйлчилгээ хамт хуваалцана.

```bash
docker compose -p dahua-telegram config --quiet
docker compose -p dahua-telegram up -d --build
docker compose -p dahua-telegram ps
docker compose -p dahua-telegram logs --tail=100 -f
```

`config --quiet` нь secret агуулсан бүрэн Compose-г дэлгэцэнд хэвлэхгүйгээр шалгана. `.env` өөрчилсний дараа `docker compose -p dahua-telegram up -d` дахин ажиллуулна. Image өөрчилсөн үед `--build` нэмнэ. Зогсоох бол энэ төслийн хавтсаас `docker compose -p dahua-telegram stop` хэрэглэнэ; Akuvox төслийн командуудыг ажиллуулахгүй.

**Порт давхцах үед:** тусдаа Dahua контейнерт `SMTP_PORT=2526`, `HTTP_PORT=8091` сонгож, MikroTik-ийн `to-ports` болон filter-ийн `dst-port`-ыг `2526` болгон өөрчилнө. Контейнерийн дотор SMTP `2525`, HTTP `8090` хэвээр. Ингэснээр дээрх архитектурын topnet host порт `2526` болно. Хэрэв заавал topnet `2525` байх шаардлагатай бол одоогийн эзэмшигч үйлчилгээний байршуулалтыг зохицуулах хэрэгтэй; хоёр контейнер нэг host IP:port дээр зэрэг bind хийж чадахгүй. Одоо ажиллаж буй Akuvox-г энэ repository-ийн код гэж таамаглан камерын урсгалыг шууд түүнд бүү шилжүүл.

topnet/provider firewall дээр TCP SMTP publish портыг зөвхөн MikroTik-ийн бодит **WAN public IP**-аас зөвшөөрнө (`192.168.1.50` нь интернетээр source хэвээр ирэхгүй). Docker publish нь зарим host firewall дүрмийг тойрдог тул provider firewall эсвэл Docker forwarding түвшний дүрэмд бодитоор хязгаарласныг гаднаас шалга. HTTP-г гаднаас хэрэглэхгүй бол loopback bind ашигла. Контейнерээс DNS болон Telegram руу HTTPS 443 гарах шаардлагатай.

### Олон камерын Subject / Sender ялгалт

Нэг SMTP listener олон камерын имэйлийг хүлээн авна. Receiver нь routing хийдэггүй: бүх имэйл TELEGRAM_CHAT_ID сувагт очно. Камерын нэрийг дараах дарааллаар сонгоно:

1. Plain text body дахь `Camera:`, `Camera Name:`, `Device:`, `Device Name:`, `Title:`, `Камер:` эсвэл `Камерын нэр:` мөр.
2. From талбарын display name.
3. Subject дахь эхний `[камерын нэр]` хэсэг.
4. From email хаяг.
5. Subject; хоосон бол DEVICE_NAME.

| Камер | Sender | Subject / Title |
|---|---|---|
| Урд хаалга | `dahua-front@camera.local` | `[Front Door] Motion` |
| Арын хаалга | `dahua-back@camera.local` | `[Back Door] Motion` |
| Агуулах | `dahua-store@camera.local` | `[Warehouse] Motion` |

Body эсвэл display name Subject-оос давуу тул бодитоор ирсэн имэйлд нэр нь давхцахгүйг шалга. CLI-аар камер бүрт тусдаа env файл, DAHUA_IP, DAHUA_SENDER, DAHUA_TITLE өгнө. Нэг SMTP endpoint-д холбогдож байгаа бүх камерын SMTP credentials bridge-ийн нэг хос утгатай таарна. Нэрийн том/жижиг үсгийг cooldown ялгахгүй. Бүх SMTP имэйл `motion` event гэж тооцогдоно; өөр камерт ижил нэр өгвөл cooldown хоорондоо нөлөөлнө. Subject-д дан цагийн тэмдэг тавихаас зайлсхий. Subject-д тусдаа `test` үг байвал туршилт гэж үзэн cooldown-ыг алгасаж, cooldown хугацааг шинэчлэхгүй.

## 5. Баталгаажуулалт

### Кодын тест

Төслийн root-оос dependency суулгасны дараа:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Linux: `.venv/bin/python -m unittest discover -v`. Тестүүд Telegram API болон camera API-г mock хийнэ; HTTP тест нь зөвхөн loopback дээр санамсаргүй чөлөөт порт нээнэ. Жинхэнэ камер, bot token, topnet холболт шаардахгүй. Production сүлжээний ажиллагааг доорх алхмаар тусад нь батална.

### Үйлчилгээнээс камер хүртэл

| Алхам | Команд / үйлдэл | Хүлээгдэх үр дүн |
|---|---|---|
| 1 | topnet: `docker compose -p dahua-telegram ps` | Running, хэсэг хугацааны дараа healthy |
| 2 | topnet: `curl -fsS http://127.0.0.1:8090/health` | `{"status": "ok"}`; энэ нь Telegram хүрэхийг шалгахгүй |
| 3 | Доорх local HTTP test | Telegram-д тестийн текст ирнэ |
| 4 | Камерын LAN Windows: `Test-NetConnection 192.168.1.245 -Port 25` | TcpTestSucceeded=True; Telegram хүргэлтийн баталгаа биш |
| 5 | Dahua Email Test | Telegram-д тест ирнэ, log-д SMTP delivered |
| 6 | Камерын өмнө бодит хөдөлгөөн | Камерын зөв нэр, Subject, зурагтай дохио ирнэ |
| 7 | 15 секунд дотор дахин хөдөлгөөн | COOLDOWN_S=15 үед тухайн камер/event давталт дарагдаж болно |
| 8 | Өөр камер хөдөлгөх | Тусдаа identity тул нөгөө камерын cooldown-д дарагдахгүй |

Local HTTP тест (topnet дээр; token-ыг `.env` дэх утгатай тааруул):

```bash
curl --get --fail 'http://127.0.0.1:8090/event' \
  --data-urlencode 'token=YOUR_WEBHOOK_TOKEN' \
  --data-urlencode 'device=Front Door' \
  --data-urlencode 'subject=Installation test' \
  --data-urlencode 'test=1'
```

Хариу `{"status":"sent"}` байвал bridge Telegram API-аас амжилттай хариу авсан. Зураг шалгахдаа `curl --fail --data-binary @snapshot.jpg -H 'Content-Type: image/jpeg' 'http://127.0.0.1:8090/event?token=YOUR_WEBHOOK_TOKEN&device=Front&test=1'` хэрэглэнэ. Multipart form биш, raw JPEG/PNG body илгээнэ. Query string-ийн `device` (`camera` нь fallback), `type`, `subject`, `test=1` утгыг хэрэглэнэ; JSON/form body-оос эдгээрийг задлахгүй. Нууц token-той URL-ыг shell history/скриншотод бүү түгээ.

MikroTik дээр тоолуур шалгах:

```routeros
/ip firewall nat print stats where comment="Dahua SMTP to topnet"
/ip firewall filter print stats where comment="Allow Dahua SMTP to topnet"
/ip firewall connection print detail where dstnat=yes
```

## 6. Алдаа оношлох

| Шинж | Шалгах зүйл / шийдэл |
|---|---|
| Docker `address already in use` | `docker ps`, `docker port akuvox-telegram`, `ss -lnt`-ээр эзэмшигчийг ол; өөр publish порт сонгоод NAT/filter/firewall-аа тааруул |
| Health ажиллахгүй | Контейнерийн төлөв, logs, HTTP_BIND/HTTP_PORT шалга; healthcheck доторх порт үргэлж 8090 |
| Email Test timeout | Камерын server=192.168.1.245, port=25; NAT counter, forward rule дараалал, WAN srcnat, topnet/provider firewall, ISP портын хориг шалга |
| NAT counter өсөхгүй | Камерын IP, SMTP destination, LAN зам, өмнөх NAT дүрэм шалга; өөрчилсний дараа шинэ холболт үүсгэ |
| NAT өсөөд filter өсөхгүй | NAT-ын дараах destination/port таарч байгаа эсэх, өмнө нь accept/drop дүрэм таарч байгааг шалга |
| SMTP authentication алдаа | Anonymous болон SMTP_USER/SMTP_PASS тохиролцсон эсэх; TLS/SSL унтраалттай эсэх; env өөрчлөлтийн дараа container recreate хийсэн эсэх |
| SMTP `451` / HTTP `502` | Telegram token, chat ID, bot-ын Post Messages эрх, DNS/HTTPS гаралт шалга; API/network алдаа бол илгээгч дахин оролдох шаардлагатай |
| SMTP `250` боловч шинэ пост алга | Cooldown байж болно: log дахь cooldown шалга; түр хүлээх эсвэл test subject ашигла |
| Текст ирээд зураг алга | Attachment, Event Snapshot, Snapshot linkage, channel, schedule шалга; Email Test өөрөө зураггүй байж болно |
| Test ажиллаад хөдөлгөөн ирэхгүй | Motion Enable, Region, Period, sensitivity, Send Email болон Snapshot linkage-ийг Web UI-аар шалга |
| Камерын нэр буруу / давталт дарагдах | Body/display name Subject-оос давуу; камер бүрийн Sender, bracketed Subject-ийг ялгаатай болго |
| CLI firmware fields missing | CLI өөрчлөлт илгээгээгүй; `--show`-оор талбараа шалгаад Web UI ашигла |
| CLI read-back mismatch | Камер хэсэгчлэн өөрчлөгдсөн байж болно; masked `--show` болон Web UI-аар шалгаж тохируул |
| CLI connection failed | LAN хүрэлт, IP, хэрэглэгч/нууц үг, camera HTTP API/Digest дэмжлэг шалга; redirect дагахгүй |
| HTTP `403`, `400`, `413`, `415` | Token, Content-Length, 20 MiB дээд хэмжээ, raw JPEG/PNG формат шалга; chunked upload дэмжихгүй |

Лог нь token агуулдаг Telegram URL, SMTP body, webhook path-ыг зориудаар хэвлэхгүй. `.env`, bot token, камерын нууц үгийг git-д бүү нэм. Үйлчилгээ дискэн queue хадгалахгүй: restart-аар cooldown арилна, алдаатай SMTP-д 451 буцаана; камер дахин оролдох эсэх нь firmware-аас хамаарна. Олон зураг хэсэгчлэн хүрсний дараа алдаа гарвал retry нь өмнөх зургийг давхардуулж болно. Зураг 10 хүртэлх багцаар, ганц зураг sendPhoto-оор явна; текст 4096, caption 1024 тэмдэгтээр хязгаарлагдана. Цагийг Улаанбаатарын UTC+8-аар харуулна. HTTP `/health` нь зөвхөн listener амьд байгааг илтгэнэ.
