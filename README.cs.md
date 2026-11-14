# ha-epsnet

[English](README.md) · Česky

Doplněk (custom integrace) pro Home Assistant, který komunikuje s PLC Tecomat Foxtrot / iNELS
přímo protokolem EPSNET přes UDP — bez brány PLCComS.

Projekt není nijak spojen se společností Teco a.s. ani jí podporován; Tecomat, Foxtrot a iNELS
jsou její ochranné známky. Podpora EPSNET vznikla kvůli interoperabilitě a je vyzkoušená na jedné
instalaci (centrální jednotka iNELS 2 CU2-01M — hardware Foxtrot první generace — s moduly níže,
Home Assistant 2026.2 a novější). Jiné centrální jednotky Foxtrot zatím nejsou vyzkoušené. Zkušenosti z jiných
instalací jsou vítané — otevřete integraci, zvolte **⋮ → Stáhnout diagnostická data** a soubor přiložte
k hlášení (issue). Obsahuje názvy proměnných z vašeho programu; klidně je přepište.

Moduly, které integrace zatím nezná, se nevytvoří jako zařízení; místo toho je uvidíte (s kódy typů)
v upozornění v **Opravách**, takže víte, co chybí.

## Co umí

- přímá komunikace EPSNET UDP, PLCComS není potřeba
- při nastavení projde PLC a zjistí, které moduly CIB jsou skutečně připojené; každé zařízení z mapy
  proměnných přiřadí ke správnému modulu podle jeho pořadí v programu PLC
- mapu publikovaných proměnných (soubor `.pub` programu) načte přímo z PLC, nebo z nahraného souboru
- bez mapy: všechny vstupy a výstupy podporovaných modulů CIB, načtené z konfigurace I/O
  uložené přímo v programu PLC, jen pro čtení
- entity pro světla, spínače, binární vstupy (tlačítka, PIR, magnetické kontakty) a teploty
- rychlé čtení binárních vstupů (např. každých 100 ms), aby se zachytil i krátký stisk tlačítka
- diagnostika a senzory s dobou čtení

Podporované moduly: `SA2-04M`, `SA2-012M`, `DA2-22M`, `WSB2-20`, `WSB2-40`, `IM2-40B`,
`IM2-140M`, `C-IT-0200I`, `C-RC-0011R` (teplota, vlhkost, externí teplota, tlačítka; podle popisu
modulu, zatím nevyzkoušeno na skutečné instalaci).

## Instalace

### Marketplace (Home Assistant 2026.11 a novější)

1. **Nastavení → Marketplace → Add from link** (přidat z odkazu), zadejte `ocervinka/ha-epsnet`
   a potvrďte.
2. Nainstalujte **EPSNET**.
3. Restartujte Home Assistant.

### HACS (Home Assistant starší než 2026.11)

1. V HACS přidejte `https://github.com/ocervinka/ha-epsnet` jako vlastní repozitář (custom
   repository) s kategorií **Integration**.
2. Nainstalujte **EPSNET**.
3. Restartujte Home Assistant.

### Ručně

Zkopírujte složku `custom_components/epsnet` do konfigurační složky Home Assistantu jako
`custom_components/epsnet` a restartujte Home Assistant.

## Nastavení

**Nastavení → Zařízení a služby → Přidat integraci → EPSNET**, potom:

1. **Připojení** — název, adresa PLC, port EPSNET UDP (výchozí `61682`), místní zdrojový port UDP
   (výchozí `61681`) a prodleva po každém čtení v milisekundách (výchozí `500`, `0` = bez prodlevy).
2. **Mapa proměnných** — **načíst z PLC** (doporučeno) nebo **nahrát exportovaný `.pub` / `.piz` /
   `.zip`**.
3. **Potvrzení** — ukáže identifikaci PLC, počet nalezených modulů a publikovaných bodů.

Mapa se uloží do konfigurace integrace a sama se znovu nenačítá: po změně programu v PLC zvolte
**Nastavit → Znovu načíst mapu proměnných z PLC**, nebo **… ze souboru (.pub)**, když PLC žádnou
mapu neobsahuje nebo jen starou (program nahraný z Mosaicu). Integrace i ID entit zůstanou; pokud se
mapa opravdu změnila, ukáže se upozornění v Opravách, a soubor, který nesedí na žádný modul PLC,
se odmítne. Tamtéž lze změnit intervaly čtení: prodlevu mezi úplnými čteními (výchozí 500 ms)
a rychlé čtení tlačítek a čidel (doporučeno 100 ms).

### Mapa proměnných

Mapa je soubor `.pub` programu: seznam publikovaných proměnných s jejich názvy a adresami v PLC.
Dává entitám vaše vlastní názvy a integrace přes ni spíná (proměnné programu `_ON` / `_OFF`).

I bez mapy může nastavení pokračovat: integrace přečte konfiguraci I/O přeloženou v programu PLC
(který modul je kde v paměti PLC) a vytvoří entitu pro každý vstup a výstup podporovaných modulů
CIB — tlačítka, vstupy, teploty, stavy relé a LED, úrovně stmívačů. Jsou **jen pro čtení**
a pojmenované podle typu modulu a adresy na sběrnici (např. *WSB2-40 1A2B Temperature*): program
PLC výstupy v každém cyklu přepisuje, takže je lze zobrazit, ale bez řídicích proměnných programu
ne spínat. Soubor `.pub` doplněný později tyto entity zachová (stejná ID entit, historie
i automatizace), dá jim názvy z vašeho programu a přidá světla a spínače, které lze ovládat.
Moduly na interní sběrnici CPU (ne CIB) zahrnuté nejsou.

S mapou se přidají i kanály, které žádná z jejích entit nezobrazuje (např. sabotážní kontakty,
stavové příznaky, místní tlačítka reléových modulů, fyzické stavy výstupů), vypnuté — zapněte ty,
které chcete. Vypnuté entity se z PLC nečtou. Když takový kanál začne zobrazovat entita znovu
načtené mapy, nebo ho program přestane umisťovat, jeho přidaná entita se odstraní.

Pokud na stejný vstupní nebo výstupní bit ukazují dvě proměnné mapy, integrace na to při startu
upozorní v logu a diagnostika je vypíše: obvykle jde o chybu exportu (např. relé 9–12 modulu
s 12 relé dostala bajt relé 1–4) a entita jedné z nich ukazuje kanál té druhé. Soubor `.pub`
exportujte znovu nebo ho opravte a znovu ho načtěte.

Když PLC dostane nový program, integrace to pozná (změní se kontrolní součet programu), počká,
až programovací nástroj skončí, a konfiguraci I/O přečte znovu; pokud se změnila, integrace se
znovu načte. Do té doby jsou entity umístěné podle programu nedostupné.
Chcete-li soubor s diagnostikou sdílet veřejně, zapněte *Nastavit → Intervaly čtení a diagnostika →
Skrýt názvy proměnných v diagnostice*: vlastní slova v názvech se nahradí zástupnými `w1`, `w2`…

- **FoxTool** ukládá `.pub` do PLC při každém nahrání programu, takže ho *načtení z PLC* najde.
- **Mosaic** ho ukládá do složky projektu v počítači, a jen když je v *Project manager → Sw → Export
  files* zapnutý *Assembler (.pub)*. Ten soubor nahrajte.

Procházení PLC a načtení mapy používají dočasný port UDP, takže se FoxTool, Mosaic i Firmware
Updater mohou k PLC připojit, i když běží Home Assistant (výhradní spojení s PLC je obsazené jen
několik minut při načítání mapy).

## Jazyky

Obrazovky nastavení, volby, upozornění a názvy diagnostických senzorů jsou anglicky, česky a slovensky;
Home Assistant použije jazyk z vašeho uživatelského profilu. Názvy světel, spínačů a vstupů se
berou z názvů proměnných v programu PLC, takže se zobrazí přesně tak, jak jsou pojmenované tam.

Podrobnosti (diagnostika, testy, omezení) jsou v [anglickém README](README.md).

## Viz také

Nepoužíváte Home Assistant, nebo potřebujete libovolné proměnné PLC v MQTT u starších zařízení,
která MQTT nepodporují? [plccoms-mqtt-bridge](https://github.com/ocervinka/plccoms-mqtt-bridge)
propojí PLCComS od Teco s MQTT brokerem (openHAB, Node-RED, Home Assistant MQTT).

## Licence

[MIT](LICENSE)
