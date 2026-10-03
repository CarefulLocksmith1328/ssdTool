# SSD-Datenretter

Windows-Tool mit Browser-Oberfläche, das auf NTFS-Laufwerken (SSD, HDD,
USB-Stick) **gelöschte Dateien findet** und sie wahlweise

- **wiederherstellt** (in einen Ordner auf einem anderen Laufwerk) oder
- **endgültig löscht**, sodass sie mit keinem Programm mehr gerettet werden können.

Die Oberfläche läuft lokal im Browser (`http://127.0.0.1:8765`). Nichts verlässt
den Rechner. Python-Pakete von Drittanbietern werden nicht benötigt.

## Starten

**Variante A: EXE (ohne Python)**
`SSD-Datenretter.exe` per Doppelklick starten. Windows fragt nach
Administratorrechten, danach öffnet sich der Browser.
Die EXE baust du mit `build_exe.bat`. Alternativ lädst du sie im GitHub-Actions-Lauf
„CI“ (Reiter *Actions*) als Artefakt `SSD-Datenretter-exe` herunter.

**Variante B: mit Python 3.10+**
```
start.bat
```
oder `python -m ssdtool` im Repository-Ordner. Ohne Administratorrechte
startet sich das Tool automatisch mit UAC-Abfrage neu.

Optionen: `--port 8765`, `--no-browser`, `--no-elevate`

## Bedienung

1. **Laufwerk wählen.** Links stehen alle Laufwerke mit Dateisystem, Typ (SSD/HDD)
   und Systemlaufwerk-Markierung. Statt eines Laufwerks kannst du auch eine
   Abbild-Datei (`.img`/`.dd`, auch ganze Datenträger mit MBR/GPT) angeben.
2. **„Nach gelöschten Dateien suchen“.** Die Suche liest die Master File Table
   (MFT) nur und verändert nichts. Für jede Datei wird geprüft, ob ihre Datenblöcke
   noch frei sind.
3. **Ergebnisliste.** Du kannst nach Name/Pfad suchen, nach Zustand oder Dateityp
   filtern und sortieren. Ein Klick auf eine Zeile zeigt Details und eine Vorschau
   (Bilder, Text oder Hex).
4. **Dateien anhaken** und dann eine Aktion wählen:
   - **Wiederherstellen …** kopiert die Dateien in einen Zielordner, wahlweise mit
     der ursprünglichen Ordnerstruktur. Das Ziel muss auf einem **anderen
     Laufwerk** liegen, sonst würden andere gelöschte Daten überschrieben.
   - **Endgültig löschen …** überschreibt die noch freien Datenblöcke mit
     Zufallsdaten und leert den MFT-Eintrag (Name, Zeitstempel, Kleinstdateien).
     Danach wird TRIM ausgeführt.
5. **Freien Speicher bereinigen** (links unten) vernichtet *alle* gelöschten Dateien
   eines Laufwerks auf einmal. Das funktioniert auch auf `C:`.

### Zustände

| Zustand | Bedeutung |
|---|---|
| **Gut** | Datenblöcke sind frei und enthalten noch Daten. Die Wiederherstellung ist sehr wahrscheinlich erfolgreich. |
| **Teilweise** | Ein Teil der Blöcke gehört schon anderen Dateien. Die Datei wird beschädigt sein. |
| **Überschrieben** | Alle Blöcke wurden neu vergeben. Nur Name und Metadaten sind noch bekannt. |
| **TRIM** | Die Blöcke sind frei, die SSD liefert aber nur noch Nullen. Die Datei ist nicht mehr zu retten. |
| **Verschlüsselt** | Die Datei war EFS-verschlüsselt. Es lassen sich nur die verschlüsselten Rohdaten sichern. |
| **Unvollständig** | Die Verwaltungsdaten (Erweiterungseinträge) fehlen teilweise. |

## Was SSDs anders machen

- **Wiederherstellen:** Ist TRIM aktiv (Standard unter Windows), meldet Windows
  gelöschte Bereiche an die SSD. Die meisten SSDs liefern für diese Bereiche
  danach nur noch Nullen. Deshalb gilt: **Je schneller nach dem Löschen gesucht
  wird, desto besser.** Rechner möglichst wenig weiterbenutzen. Ob TRIM aktiv ist,
  zeigt die Kopfzeile. Kleine Dateien (bis etwa 700 Byte) liegen direkt im
  MFT-Eintrag und sind von TRIM nicht betroffen.
- **Endgültig löschen:** Wegen Wear-Leveling schreibt eine SSD neue Daten
  physisch oft an eine andere Stelle. Über TRIM werden die alten Speicherzellen aber
  als ungültig markiert und vom Controller gelöscht. Über die normale
  Laufwerksschnittstelle kommt danach niemand mehr an die Daten. Wer sich auch
  gegen das Auslesen der Flash-Chips im Labor absichern muss, braucht
  *ATA Secure Erase* bzw. *NVMe Sanitize* (Hersteller-Tool, löscht die ganze SSD)
  oder durchgehende Verschlüsselung (BitLocker).

## Grenzen

- Nur **NTFS**. FAT32/exFAT (oft bei USB-Sticks und Speicherkarten) wird nicht
  unterstützt.
- Gezieltes Löschen einzelner Dateien braucht exklusiven Zugriff auf das
  Laufwerk. Auf dem **Windows-Systemlaufwerk** ist das nicht möglich. Nutze dort
  „Freien Speicher bereinigen“.
- Dateinamen können zusätzlich noch im NTFS-Journal (`$LogFile`, `$UsnJrnl`),
  in Verzeichnis-Indexresten und in **Schattenkopien** (Systemwiederherstellung)
  stehen. Schattenkopien löschst du mit `vssadmin delete shadows /for=C:`.
- Dateien ohne MFT-Eintrag (z. B. nach dem Formatieren) findet das Tool nicht,
  denn es gibt keine Signatursuche („Carving“).
- Alternative Datenströme (ADS) werden nicht wiederhergestellt.

## Aufbau

```
ssdTool/
├── start.bat / build_exe.bat / run.py
├── ssdtool/
│   ├── device.py    sektor-ausgerichteter Rohzugriff auf \\.\X: und Abbilder, Volume-Lock
│   ├── ntfs.py      NTFS-Parser: Bootsektor, MFT, Fixups, Datenläufe, $ATTRIBUTE_LIST, Bitmap, LZNT1
│   ├── scanner.py   gelöschte Einträge finden, Pfade rekonstruieren, Zustand bewerten
│   ├── recover.py   Wiederherstellung (inkl. NTFS-komprimierter Dateien)
│   ├── wipe.py      gezieltes Überschreiben + Bereinigung des freien Speichers
│   ├── winutil.py   Admin-Rechte, Laufwerksliste, SSD-/TRIM-Erkennung, ReTrim
│   ├── server.py    lokaler HTTP-Server mit Token-Schutz
│   └── web/         Oberfläche (HTML/CSS/JS)
└── tests/           Tests mit echten NTFS-Abbildern und Windows-E2E-Test
```

Sicherheit der lokalen Oberfläche: Der Server lauscht nur auf `127.0.0.1`.
Jede API-Anfrage braucht ein zufälliges Token, das beim Start erzeugt wird.
Fremde Webseiten im Browser können deshalb keine Aktionen auslösen.
Löschaktionen müssen zusätzlich durch Eintippen von `LÖSCHEN` bestätigt werden.

## Tests

```
python -m pytest tests          # Linux, root + ntfs-3g: echte NTFS-Abbilder
python tests\windows_e2e.py     # Windows, als Administrator: VHD-Laufwerk
```

Der GitHub-Actions-Workflow `.github/workflows/ci.yml` führt beides aus und baut
die EXE.
