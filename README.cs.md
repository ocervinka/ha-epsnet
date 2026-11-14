# ha-epsnet

[English](README.md) · Česky

Vlastní integrace (custom integration) pro Home Assistant, která komunikuje s PLC Tecomat Foxtrot /
iNELS přímo protokolem EPSNET přes UDP, bez brány PLCComS.

Projekt není nijak spojen se společností Teco a.s. ani jí podporován; Tecomat, Foxtrot a iNELS
jsou její ochranné známky. Podpora EPSNET vznikla kvůli interoperabilitě a je vyzkoušená na jedné
instalaci (centrální jednotka iNELS 2 CU2-01M, tedy hardware Foxtrot první generace, s moduly
uvedenými níže, Home Assistant 2026.2 a novější). Jiné centrální jednotky Foxtrot zatím vyzkoušené
nejsou. Zkušenosti z jiných instalací jsou vítané: otevřete integraci, zvolte **Nastavit →
Stáhnout diagnostiku** a soubor přiložte k hlášení (issue). Vaše vlastní názvy proměnných jsou
v něm skryté, pokud to nezměníte.

Moduly, které integrace zatím nezná, se jako zařízení nevytvoří. Najdete je i s kódy typů
v upozornění v **Opravách**, takže víte, co chybí.

## Co umí

- komunikuje přímo protokolem EPSNET přes UDP, PLCComS není potřeba
- při nastavení projde PLC a zjistí, které moduly CIB jsou skutečně připojené; každé zařízení z mapy
  proměnných přiřadí ke správnému modulu podle jeho pořadí v programu PLC
- mapu publikovaných proměnných (soubor `.pub` k programu) načte přímo z PLC, nebo z nahraného souboru
- bez mapy zobrazí stavy všech vstupů a výstupů podporovaných modulů CIB podle konfigurace I/O
  uložené přímo v programu PLC (bez ovládání)
- entity pro světla, spínače, binární vstupy (tlačítka, PIR, magnetické kontakty) a teploty
- binární vstupy čte zvlášť a častěji (např. každých 100 ms), aby zachytila i krátký stisk tlačítka
- diagnostika a senzory s dobou čtení

Podporované moduly: `SA2-04M`, `SA2-012M`, `DA2-22M`, `WSB2-20`, `WSB2-40`, `IM2-40B`,
`IM2-140M`, `C-IT-0200I`, `C-RC-0011R` (teplota, vlhkost, externí teplota, tlačítka; podle popisu
modulu, zatím nevyzkoušeno na skutečné instalaci).

## Instalace

### Marketplace (Home Assistant 2026.11 a novější)

1. **Nastavení → Marketplace → Add from link** (přidat z odkazu), zadejte `ocervinka/ha-epsnet`
   a potvrďte.
2. Nainstalujte **Teco Foxtrot / iNELS 2 (EPSNET)**.
3. Restartujte Home Assistant.

### HACS (Home Assistant starší než 2026.11)

1. V HACS přidejte `https://github.com/ocervinka/ha-epsnet` jako vlastní repozitář (custom
   repository) s kategorií **Integration**.
2. Nainstalujte **Teco Foxtrot / iNELS 2 (EPSNET)**.
3. Restartujte Home Assistant.

### Ručně

Zkopírujte složku `custom_components/epsnet` do konfigurační složky Home Assistantu jako
`custom_components/epsnet` a restartujte Home Assistant.

## Nastavení

**Nastavení → Zařízení a služby → Přidat integraci**, hledejte *Teco*, *Foxtrot*, *iNELS* nebo
*EPSNET*.

1. **Vyberte PLC.** Integrace prohledá síť a vypíše nalezená PLC i s jejich typem. Vyberte to své
   a zvolte **Připojit**. Pokud se žádné nenajde (hledání funguje jen ve stejné síti, v jaké je
   Home Assistant), zadejte adresu PLC ručně. Oba porty UDP jsou v části *Rozšířené nastavení*
   a obvykle se nemění (port PLC `61682`, místní port `61681`).
2. **Počkejte.** Integrace se připojí, projde připojené moduly a načte názvy proměnných uložené
   v PLC. Typ PLC a počet modulů ukáže hned, jakmile je zjistí.
3. **Hotovo.** Home Assistant vypíše moduly a nabídne jejich přiřazení do místností.

Home Assistant může PLC najít i sám: objeví se pak na stránce integrací pod *Zjištěno*
a po zvolení **Přidat** začne nastavení krokem 2.

Na další věci se nastavení ptá jen tehdy, když je z PLC samo nezjistí:

- **V PLC nejsou uložené názvy proměnných** (viz níže): nahrajte soubor `.pub` k programu, nebo
  pokračujte bez něj. Home Assistant pak jen zobrazuje stavy a nemůže nic ovládat, dokud soubor
  nedoplníte.
- **PLC právě používá jiný program** (například programovací nástroj): zavřete ho a zkuste to
  znovu, nebo raději nahrajte soubor `.pub`.
- **Nahraný soubor neodpovídá žádnému z modulů:** nejspíš je z jiného projektu. Nahrajte jiný, nebo
  ho použijte i tak.

Názvy proměnných se uloží do konfigurace integrace a samy se znovu nenačítají. Po změně programu
v PLC zvolte **Nastavit → Znovu načíst názvy proměnných z PLC**, nebo **Načíst názvy proměnných ze
souboru .pub**, když v PLC názvy nejsou nebo jsou zastaralé (program nahraný z Mosaicu). Integrace
i ID entit zůstanou. Pokud se názvy opravdu změnily, objeví se upozornění v Opravách. Soubor,
který neodpovídá žádnému modulu PLC, integrace odmítne. V **Nastavit → Rychlost čtení** lze změnit
pauzu po přečtení všech hodnot (výchozí 500 ms) a interval čtení tlačítek a čidel (u nových
instalací 100 ms); formulář ukazuje i doby čtení změřené na vaší instalaci.

### Mapa proměnných

Mapa je soubor `.pub` k programu: seznam publikovaných proměnných s jejich názvy a adresami v PLC.
Entity podle ní dostanou vaše vlastní názvy a integrace přes její proměnné (`_ON` / `_OFF`
v programu) ovládá výstupy.

Nastavení může pokračovat i bez mapy. Integrace pak přečte konfiguraci I/O, která je součástí
programu PLC (který modul leží kde v paměti PLC), a vytvoří entitu pro každý vstup a výstup
podporovaných modulů CIB: tlačítka, vstupy, teploty, stavy relé a LED, úrovně stmívačů. Tyto
entity **jen zobrazují stav** a jmenují se podle typu modulu a adresy na sběrnici (např. *WSB2-40
1A2B Temperature*). Program PLC totiž výstupy v každém cyklu přepisuje, takže je lze zobrazit, ale
bez řídicích proměnných programu ne ovládat. Když soubor `.pub` doplníte později, tyto entity
zůstanou (stejná ID, historie i automatizace), dostanou názvy z vašeho programu a přibudou světla
a spínače, které lze ovládat. Moduly na interní sběrnici CPU (ne CIB) zahrnuté nejsou.

S mapou integrace přidá i kanály, které žádná z entit mapy nezobrazuje (např. sabotážní kontakty,
stavové příznaky, místní tlačítka reléových modulů, fyzické stavy výstupů). Jsou vypnuté; zapněte
si ty, které chcete. Vypnuté entity se z PLC nečtou. Když takový kanál začne zobrazovat entita
z nově načtené mapy, nebo ho program přestane používat, přidaná entita se odstraní.

Pokud dvě proměnné mapy ukazují na stejný vstupní nebo výstupní bit, integrace na to při startu
upozorní v logu a diagnostika je vypíše. Obvykle jde o chybu exportu (např. relé 9–12 modulu
s 12 relé dostala bajt relé 1–4) a entita jedné proměnné pak ukazuje kanál té druhé. Soubor `.pub`
exportujte znovu nebo ho opravte a načtěte ho znovu.

Když PLC dostane nový program, integrace to pozná (změní se kontrolní součet programu), počká,
až programovací nástroj skončí, a konfiguraci I/O přečte znovu. Pokud se změnila, integrace se
znovu načte. Do té doby jsou entity vytvořené podle programu nedostupné.

Diagnostický soubor stáhnete v **Nastavit → Stáhnout diagnostiku**. Volba *Skrýt názvy proměnných*
je zaškrtnutá, takže soubor můžete sdílet veřejně: vlastní slova v názvech se nahradí značkami
`w1`, `w2`… Když zaškrtnutí zrušíte, zůstanou v souboru skutečné názvy. Volba se zapamatuje.

- **FoxTool** ukládá `.pub` do PLC při každém nahrání programu, takže ho tam nastavení najde.
- **Mosaic** ho ukládá do složky projektu v počítači, a jen když je v okně *Manažer projektu → Sw →
  Exportní soubory* (anglicky *Project manager → Sw → Export files*) zaškrtnutá volba *Assembler
  (.pub)*. Tento soubor nahrajte, až si o něj nastavení řekne.

Procházení PLC a načítání mapy používají dočasný port UDP, takže se FoxTool, Mosaic i Firmware
Updater mohou k PLC připojit, i když Home Assistant běží (výhradní spojení s PLC je obsazené jen
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
