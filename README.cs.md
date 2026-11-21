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
- při nastavení projde PLC a zjistí, které moduly CIB jsou skutečně připojené; proměnné z mapy
  přiřadí k modulům podle adresy (proměnná patří modulu, jehož vstupem nebo výstupem je), takže
  názvy nemusí mít žádný předepsaný tvar
- mapu publikovaných proměnných (soubor `.pub` k programu) načte přímo z PLC, nebo z nahraného souboru
- bez mapy vytvoří entity pro všechny vstupy a výstupy podporovaných modulů CIB podle konfigurace
  I/O uložené přímo v programu PLC
- ovládání bez povelových proměnných: tlačítka nástěnných ovladačů lze stisknout z Home Assistantu
  a relé, výstupy stmívačů a signalizační LED ovládat tlačítky přímo na modulech nebo zápisem
  přímo do výstupu
- entity pro světla, spínače, tlačítka, binární vstupy (tlačítka, PIR, magnetické kontakty) a teploty
- ostatní zveřejněné proměnné programu (příznaky, žádané hodnoty, povely) jako entity, které si
  povolíte podle potřeby
- binární vstupy čte zvlášť a častěji (např. každých 100 ms), aby zachytila i krátký stisk tlačítka
- diagnostika a senzory s dobou čtení

Podporované moduly: `SA2-04M`, `SA2-012M`, `DA2-22M`, `WSB2-20`, `WSB2-40`, `IM2-40B`,
`IM2-140M`, `C-IT-0200I`, `C-RC-0011R` (teplota, vlhkost, externí teplota, tlačítka) a `C-IR-0303M`
(3 vstupy, 3 analogové vstupy zobrazené jako teploty, 3 reléové výstupy). Poslední dva jsou
doplněné podle popisu modulu a zatím nevyzkoušené na skutečné instalaci.

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
  pokračujte bez něj. Home Assistant pak všechno pojmenuje podle typu modulu a adresy na sběrnici
  a ovládá jen to, co program v PLC dovolí (viz „Ovládání bez povelových proměnných“ níže), dokud
  soubor nedoplníte.
- **PLC právě používá jiný program** (například programovací nástroj): zavřete ho a zkuste to
  znovu, nebo raději nahrajte soubor `.pub`.
- **Nahraný soubor neodpovídá žádnému z modulů** (žádná jeho proměnná není vstupem ani výstupem
  modulu): nejspíš je z jiného projektu. Nahrajte jiný, nebo ho použijte i tak, pokud program
  opravdu zveřejňuje jen své vnitřní proměnné. Budou z nich zakázané entity popsané níže.

Názvy proměnných se uloží do konfigurace integrace a samy se znovu nenačítají, protože jejich
čtení obsadí servisní spojení s PLC. Když PLC dostane nový program, objeví se upozornění
v **Nastavení → Opravy**: dokud názvy nenačtete znovu, chybí světla a čidla, která jste přidali,
a hodnoty i ovládání mohou být chybné, pokud nový program umístil proměnné v paměti PLC jinam.
Upozornění nabídne načíst názvy znovu z PLC (nebo nahrát nový soubor `.pub`, pokud názvy pocházejí
ze souboru nebo v PLC žádné nejsou), případně stávající názvy ponechat, když má nový program úplně
stejné proměnné. Po načtení ukáže, kolik proměnných přibylo, ubylo a kolik se jich přesunulo.

Totéž jde udělat kdykoli ručně: **Nastavit → Znovu načíst názvy proměnných z PLC**, nebo **Načíst
názvy proměnných ze souboru .pub**, když v PLC názvy nejsou nebo jsou zastaralé (program nahraný
z Mosaicu). Integrace i ID entit zůstanou. Pokud se názvy opravdu změnily, objeví se upozornění
v Opravách. Soubor, který neodpovídá žádnému modulu PLC, integrace odmítne. V **Nastavit → Rychlost
čtení** lze změnit
pauzu po přečtení všech hodnot (výchozí 500 ms) a interval čtení tlačítek a čidel (u nových
instalací 100 ms); formulář ukazuje i doby čtení změřené na vaší instalaci.

### Mapa proměnných

Mapa je soubor `.pub` k programu: seznam publikovaných proměnných s jejich názvy a adresami v PLC.
Entity podle ní dostanou vaše vlastní názvy a integrace přes její proměnné (`_ON` / `_OFF`
v programu) ovládá výstupy.

**Názvy se přiřazují podle adresy.** Z programu PLC je vidět, kde v paměti PLC leží vstupy
a výstupy každého modulu. Proměnná z mapy, která tam leží, tedy patří tomuto modulu a pojmenuje
jeho vstup nebo výstup, ať se jmenuje jakkoli. Mají-li proměnné jednoho modulu společný začátek
názvu (`SW_hall_UP1`, `SW_hall_TERM`…), pojmenuje tento začátek modul (*SW_hall*) a zbytek kanál
(*UP1*). Nemají-li žádný, modul se dál jmenuje podle typu a adresy na sběrnici (*WSB2-40 1A2B*)
a jeho entity nesou celé názvy proměnných. Jen když se konfiguraci I/O z programu nepodaří
přečíst, seskupí integrace proměnné podle názvů a skupiny přiřadí modulům podle pořadí; k tomu
jsou potřeba názvy v podobě, jakou vytváří FoxTool.

**K ovládání přes program jsou potřeba povelové proměnné.** Program PLC své výstupy obvykle
nastavuje v každém cyklu, takže hodnota, kterou do takového výstupu zapíše Home Assistant, hned
zmizí. Home Assistant proto o přepnutí žádá program. Jde to, když mapa pro výstup obsahuje tyto
proměnné (příklad pro název `Lamp`):

| Proměnná | Kde | K čemu je |
| --- | --- | --- |
| `state_Lamp` | přímo výstup (`Y`) | říká, který výstup kterého modulu to je |
| `Lamp` | paměť programu (`R`), `BOOL` | stav, který Home Assistant zobrazuje |
| `Lamp_ON`, `Lamp_OFF` | paměť programu (`R`), `BOOL` | Home Assistant jednu z nich nastaví na 1; program výstup přepne a proměnnou vynuluje |

Programy z FoxToolu je zveřejňují pro každý výstup. Do vlastního programu si je doplníte sami,
spolu s několika řádky, které na ně reagují. Entita je světlo, pokud název obsahuje `light`, jinak
spínač. U výstupu stmívače je `Lamp` typu `REAL` a obsahuje úroveň v procentech; tu Home Assistant
zapisuje při nastavení jasu. Výstup bez těchto proměnných se místo toho ovládá
přímo (další odstavec). Pokud mapa neobsahuje žádné, napíše to nastavení ve svém závěrečném
shrnutí.

**Ovládání bez povelových proměnných.** Umožňují ho dvě vlastnosti PLC, s mapou i bez ní:

- *Vstup* se v každém cyklu načítá z modulu. Zapsaná 1 v něm vydrží jen do té doby, takže program
  uvidí jeden krátký stisk.
- *Výstup* si zapsanou hodnotu ponechá, pokud ho program sám nenastavuje. Většina programů výstupy,
  které řídí, nastavuje v každém cyklu, a zapsaná hodnota pak hned zmizí.

Integrace je využívá takto:

- **Tlačítka nástěnných ovladačů.** Každé tlačítko má vedle svého senzoru i entitu tlačítka.
  Když ji v Home Assistantu stisknete, program udělá totéž, co při stisku skutečného tlačítka.
  Jde jen o krátký stisk, dlouhý ani dvojitý takto udělat nelze.
- **Relé.** Modul relé má pro každý výstup malé tlačítko přímo na sobě a programy z FoxToolu jím
  přepínají výstup se stejným číslem. Relé bez povelových proměnných je proto spínač: Home
  Assistant výstup přečte, tlačítko stiskne jen tehdy, když výstup není v požadovaném stavu,
  a výstup přečte znovu. Pokud stisk nic nezměnil, zapíše hodnotu přímo do výstupu a přečte ho
  ještě jednou.
- **Výstupy stmívačů.** Výstup stmívače bez povelových proměnných je světlo. Zapnutí a vypnutí
  funguje jako u relé (stisk tlačítka na modulu nastaví výstup na 100 % nebo na 0). Jas se do
  výstupu zapisuje jako úroveň.
- **Signalizační LED.** LED nástěnného ovladače bez povelových proměnných je spínač, který
  zapisuje přímo do výstupu.

Pokud výstup ani potom není v požadovaném stavu, řídí ho program v PLC sám: entita ohlásí chybu
a dál ukazuje skutečný stav. LED nebo jas tedy takto nastavíte jen v programu, který daný výstup
sám nenastavuje.

Výstup, který povelové proměnné má, se ovládá přes ně, nikdy těmito způsoby. Ve vlastním programu
stačí pro každý výstup jeden řádek, který ho tlačítkem na modulu přepne. Změřeno na centrální
jednotce CU2-01M (firmware 7.2): stisk s programem z FoxToolu i s ručně psanými programy, na
modulech SA2-012M, SA2-04M/Sn a DA2-22M; zápis do výstupu s ručně psaným programem, na jednom relé
(hodnota zůstala) a na jedné LED, kterou program nastavuje (hodnota nezůstala). Zápis úrovně do
výstupu stmívače zatím na PLC vyzkoušený není. Jiné CPU nebo firmware se může chovat jinak; dejte
prosím vědět, co pozorujete.

**Ostatní proměnné programu.** Všechno ostatní, co program zveřejňuje (příznaky, režimy, žádané
hodnoty, povely pod vlastními názvy), nabídne integrace jako entity, které přímo odpovídají hodnotě
proměnné. Patří k zařízení, které představuje samotné PLC a jmenuje se podle jeho typu:

| Proměnná | Entita |
| --- | --- |
| `BOOL` v paměti programu (`R`) | spínač: zapnutí zapíše 1, vypnutí 0 |
| číslo v paměti programu (celočíselné typy, `REAL`, `LREAL`) | číslo: zobrazuje hodnotu a umožňuje ji nastavit |
| zveřejněná jen pro čtení (`PUB_OUT`) nebo mimo paměť programu | binární senzor nebo senzor |

Tyto entity vznikají **zakázané**: povel se od stavového příznaku rozeznat nedá a program může
zveřejňovat stovky proměnných. Na stránce zařízení si povolte ty, které potřebujete; teprve pak je
integrace začne číst. Stejně se ovládá i program, který nemá proměnné `_ON` / `_OFF`: povolíte
proměnné, přes které přijímá povely. Home Assistant zapíše hodnotu tak, jak je, a co s ní program
udělá, záleží na programu. Povel, který si program sám vynuluje, se za okamžik zase zobrazí jako
vypnutý. Entita, kterou jste nikdy nepovolili, zmizí, jakmile program její proměnnou přestane
zveřejňovat. Proměnné typu čas, datum a text entity nemají.

Nastavení může pokračovat i bez mapy. Integrace pak přečte konfiguraci I/O, která je součástí
programu PLC (který modul leží kde v paměti PLC), a vytvoří entitu pro každý vstup a výstup
podporovaných modulů CIB: tlačítka, vstupy, teploty, relé, LED a výstupy stmívačů. Entity
se jmenují podle typu modulu a adresy na sběrnici (např. *WSB2-40 1A2B Temperature*). Co Home
Assistant dokáže ovládat bez povelových proměnných programu, závisí na programu (viz „Ovládání
bez povelových proměnných“ výše). Když soubor `.pub`
doplníte později, tyto entity zůstanou (stejná ID, historie i automatizace), dostanou názvy
z vašeho programu a přibudou světla a spínače, pro které má program povelové proměnné. Výjimkou je
výstup, který je dostane: jeho vlastní spínač nebo světlo nahradí senzor vedle nového světla nebo
spínače, takže automatizace, které používaly ten původní, je potřeba přepsat na novou entitu. Moduly na interní sběrnici CPU (ne CIB) zahrnuté
nejsou.

S mapou integrace přidá i kanály, které žádná z entit mapy nezobrazuje (např. sabotážní kontakty,
stavové příznaky, místní tlačítka reléových modulů, fyzické stavy výstupů). U modulu, který mapa
pojmenovala, jsou vypnuté; zapněte si ty, které chcete. U modulu známého jen podle typu a adresy
na sběrnici jsou zapnuté. Kanál, pro který má mapa proměnnou, nese její název. Vypnuté entity se
z PLC nečtou. Když takový kanál začne zobrazovat entita z nově načtené mapy, nebo ho program
přestane používat, přidaná entita se odstraní.

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
