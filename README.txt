WEIGHLABEL  -  Bluetooth scale -> label -> Niimbot printer   (offline, Windows)

1) FIRST-TIME SETUP (internet needed only once)
   - Install Python 3.10+ from python.org (tick "Add Python to PATH").
   - Double-click build_exe.bat  -> creates dist\WeighLabel.exe
     Copy WeighLabel.exe to any Windows laptop. It needs no internet / no Python.
   (Quick try without building: pip install -r requirements.txt, then run.bat)

2) PAIR DEVICES in Windows Bluetooth settings
   - Scale: pair it, then Bluetooth settings > More Bluetooth options > COM Ports
     tab: note the "Outgoing" COM port (e.g. COM5).
   - Printer: USB -> it appears as a COM port automatically.
              Bluetooth -> pair it, use its Outgoing COM port too.
   In the app: Refresh ports, pick the port, press Connect.

3) USE
   - Try "Demo scale" first to see everything working without hardware.
   - Choose/type Product name, enter P.Wt (kg), Size. "Save product" stores it as a preset.
   - Put the item on the scale. When the bar turns green (STABLE), press PRINT LABEL.
   - N.Wt = G.Wt - P.Wt, calculated automatically. Printing is blocked if G.Wt <= P.Wt.
   - QR content: PRODUCT|G:..|P:..|N:..|S:size|date time|#serial
   - Every label is logged (Printed labels table); Export CSV opens in Excel.
   - "Auto-print when weight is stable" is OFF by default (turn on after manual testing).
   - Data is stored in %APPDATA%\WeighLabel (settings.json, weighlabel.db).

4) IF SOMETHING IS OFF (Settings button)
   - Weight not showing / garbage: change Scale baud rate (try 9600, 4800, 19200, 115200).
     The grey "Raw scale data" line under the preview shows exactly what the scale sends.
   - Scale sends grams but shows wrong value: set Scale unit to g (or kg).
   - Label prints sideways or cut: change Rotate (0/90/180/270) and Print head width
     (B21/B1 = 384, D11/D110 = 96, B18 = 384). Label width/height in mm.
   - Too light/dark: Print darkness 1-5.
