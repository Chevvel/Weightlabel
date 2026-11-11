"""WeighLabel - Bluetooth scale -> label -> Niimbot printer. Fully offline."""
from builtins import Exception
import queue
import random
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

import core

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover
    list_ports = None


def ports():
    if not list_ports:
        return []
    return [f"{p.device} - {p.description}" for p in sorted(list_ports.comports())]


def port_name(text):
    return text.split(" - ")[0].strip()


class ScaleReader(threading.Thread):
    """Reads lines from the scale COM port (Bluetooth SPP shows up as one)."""

    def __init__(self, port, baud, q, demo=False):
        super().__init__(daemon=True)
        self.port, self.baud, self.q, self.demo = port, baud, q, demo
        self.stop = threading.Event()

    def run(self):
        try:
            self._demo() if self.demo else self._serial()
        except Exception as e:
            self.q.put(("scale_err", str(e)))

    def _serial(self):
        import serial
        ser = serial.Serial(self.port, self.baud, timeout=0.2)
        self.q.put(("scale_ok", self.port))
        buf, last = "", time.time()
        while not self.stop.is_set():
            chunk = ser.read(ser.in_waiting or 1).decode("ascii", "ignore")
            if chunk:
                buf += chunk
                last = time.time()
            parts = re.split(r"[\r\n]+", buf)
            buf = parts.pop()
            if buf and time.time() - last > 0.35:   # scale sends no line end
                parts.append(buf)
                buf = ""
            for p in parts:
                if p.strip():
                    self.q.put(("scale_line", p))
        ser.close()

    def _demo(self):
        self.q.put(("scale_ok", "DEMO"))
        target, cur = 0.0, 0.0
        t0 = time.time()
        while not self.stop.is_set():
            ph = (time.time() - t0) % 14
            target = 0.0 if ph < 3 else (12.45 if ph < 14 else 0.0)
            if ph < 3:
                target = 0.0
            cur += (target - cur) * 0.35 + (random.uniform(-0.05, 0.05) if abs(target - cur) > 0.02 else 0)
            if abs(target - cur) < 0.004:
                cur = target
            flag = "ST" if abs(target - cur) < 0.004 else "US"
            self.q.put(("scale_line", f"{flag},GS,{cur:8.3f}kg"))
            time.sleep(0.2)


class SettingsDialog(tk.Toplevel):
    FIELDS = [("Scale baud rate", "scale_baud", int), ("Scale unit (auto/kg/g)", "scale_unit", str),
              ("Printer baud rate", "printer_baud", int), ("Label width (mm)", "label_w_mm", float),
              ("Label height (mm)", "label_h_mm", float), ("Printer DPI", "dpi", int),
              ("Print head width (px)", "head_px", int), ("Rotate image (0/90/180/270)", "rotate", int),
              ("Print darkness (1-5)", "density", int), ("Decimals", "decimals", int),
              ("Stable readings needed", "stable_count", int)]

    def __init__(self, master, s, on_ok):
        super().__init__(master)
        self.title("Settings")
        self.s, self.on_ok, self.vars = s, on_ok, {}
        for i, (lab, key, typ) in enumerate(self.FIELDS):
            ttk.Label(self, text=lab).grid(row=i, column=0, sticky="w", padx=10, pady=3)
            v = tk.StringVar(value=str(s[key]))
            ttk.Entry(self, textvariable=v, width=12).grid(row=i, column=1, padx=10)
            self.vars[key] = (v, typ)
        ttk.Button(self, text="Save", command=self.ok).grid(columnspan=2, pady=10)
        self.transient(master)
        self.grab_set()

    def ok(self):
        try:
            for k, (v, t) in self.vars.items():
                self.s[k] = t(v.get())
        except ValueError:
            messagebox.showerror("Settings", "Please enter valid numbers.", parent=self)
            return
        self.on_ok()
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WeighLabel - Weighing & Label Printing")
        self.geometry("1100x800"); self.minsize(1000, 760)
        self.s = core.load_settings()
        self.store = core.Store()
        self.q = queue.Queue()
        self.scale = None
        self.printer = None
        self.stab = core.StabilityTracker(self.s["stable_count"], self.s["stable_tol"])
        self.gross, self.stable = None, False
        self.last_rec, self.armed = None, True
        self.last_printed_gross = None
        self._build()
        self.refresh_ports()
        self.refresh_products()
        self.refresh_history()
        self.update_preview()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------ UI
    def _build(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except tk.TclError:
            st.theme_use("clam")
        top = ttk.LabelFrame(self, text="Connections")
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.scale_cb = ttk.Combobox(top, width=38, state="readonly")
        self.printer_cb = ttk.Combobox(top, width=38, state="readonly")
        ttk.Label(top, text="Scale (Bluetooth COM):").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        self.scale_cb.grid(row=0, column=1)
        self.scale_btn = ttk.Button(top, text="Connect", command=self.toggle_scale)
        self.scale_btn.grid(row=0, column=2, padx=4)
        self.demo_var = tk.BooleanVar()
        ttk.Checkbutton(top, text="Demo scale", variable=self.demo_var).grid(row=0, column=3, padx=6)
        ttk.Label(top, text="Printer (USB / Bluetooth COM):").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        self.printer_cb.grid(row=1, column=1)
        self.printer_btn = ttk.Button(top, text="Connect", command=self.toggle_printer)
        self.printer_btn.grid(row=1, column=2, padx=4)
        ttk.Button(top, text="Refresh ports", command=self.refresh_ports).grid(row=0, column=4, padx=6)
        ttk.Button(top, text="Settings", command=self.open_settings).grid(row=1, column=4, padx=6)
        self.status = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status, foreground="#555").grid(row=2, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 4))

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="y")
        wf = ttk.LabelFrame(left, text="Live weight (G.Wt)")
        wf.pack(fill="x", pady=4)
        self.w_var = tk.StringVar(value="--")
        tk.Label(wf, textvariable=self.w_var, font=("Segoe UI", 46, "bold"), width=9).pack()
        self.stab_lbl = tk.Label(wf, text="WAITING", font=("Segoe UI", 12, "bold"), fg="white", bg="#888")
        self.stab_lbl.pack(fill="x", padx=8, pady=(0, 8))

        ff = ttk.LabelFrame(left, text="Product")
        ff.pack(fill="x", pady=4)
        self.name_var, self.tare_var = tk.StringVar(), tk.StringVar()
        self.size_var, self.copies_var = tk.StringVar(), tk.StringVar(value="1")
        self.net_var = tk.StringVar(value="--")
        ttk.Label(ff, text="Product name").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.name_cb = ttk.Combobox(ff, textvariable=self.name_var, width=24)
        self.name_cb.grid(row=0, column=1, padx=6)
        self.name_cb.bind("<<ComboboxSelected>>", self.pick_product)
        for i, (lab, var) in enumerate([("P.Wt (kg)", self.tare_var), ("Size", self.size_var), ("Copies", self.copies_var)], 1):
            ttk.Label(ff, text=lab).grid(row=i, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(ff, textvariable=var, width=26).grid(row=i, column=1, padx=6)
        ttk.Label(ff, text="N.Wt (kg)").grid(row=4, column=0, sticky="w", padx=6, pady=3)
        tk.Label(ff, textvariable=self.net_var, font=("Segoe UI", 16, "bold"), fg="#0a6").grid(row=4, column=1, sticky="w", padx=6)
        bf = ttk.Frame(ff)
        bf.grid(row=5, columnspan=2, pady=6)
        ttk.Button(bf, text="Save product", command=self.save_product).pack(side="left", padx=3)
        ttk.Button(bf, text="Delete product", command=self.delete_product).pack(side="left", padx=3)
        for v in (self.name_var, self.tare_var, self.size_var):
            v.trace_add("write", lambda *a: self.update_preview())

        pf = ttk.Frame(left)
        pf.pack(fill="x", pady=4)
        self.print_btn = tk.Button(pf, text="PRINT LABEL", font=("Segoe UI", 16, "bold"), bg="#1f8f3a", fg="white",
                                   height=2, command=self.print_clicked)
        self.print_btn.pack(fill="x")
        row = ttk.Frame(pf)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Reprint last", command=self.reprint).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Test print", command=self.test_print).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Save PNG", command=self.save_png).pack(side="left", expand=True, fill="x")
        self.auto_var = tk.BooleanVar(value=self.s["auto_print"])
        ttk.Checkbutton(pf, text="Auto-print when weight is stable", variable=self.auto_var).pack(anchor="w")

        right = ttk.LabelFrame(mid, text="Label preview (50 x 25 mm)")
        right.pack(side="left", fill="both", expand=True, padx=10, pady=4)
        self.prev = tk.Label(right, bg="#e3e6f0")
        self.prev.pack(fill="both", expand=True, padx=8, pady=8)
        self.raw = tk.StringVar(value="Raw scale data: -")
        ttk.Label(right, textvariable=self.raw, foreground="#777").pack(anchor="w", padx=8, pady=(0, 6))

        hf = ttk.LabelFrame(self, text="Printed labels")
        hf.pack(fill="both", padx=10, pady=(4, 10))
        cols = ("Serial", "Date/Time", "Product", "G.Wt", "P.Wt", "N.Wt", "Size", "Copies")
        self.tree = ttk.Treeview(hf, columns=cols, show="headings", height=5)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=90 if c != "Date/Time" else 150, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        ttk.Button(hf, text="Export CSV", command=self.export_csv).pack(side="right", padx=8)

    # ------------------------------------------------------------ helpers
    def refresh_ports(self):
        p = ports()
        for cb, key in ((self.scale_cb, "scale_port"), (self.printer_cb, "printer_port")):
            cb["values"] = p
            for item in p:
                if port_name(item) == self.s[key]:
                    cb.set(item)

    def refresh_products(self):
        self.name_cb["values"] = list(self.store.products())

    def refresh_history(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.store.recent():
            self.tree.insert("", "end", values=r)

    def open_settings(self):
        SettingsDialog(self, self.s, self.apply_settings)

    def apply_settings(self):
        core.save_settings(self.s)
        self.stab = core.StabilityTracker(self.s["stable_count"], self.s["stable_tol"])
        self.update_preview()

    def tare(self):
        try:
            return float(self.tare_var.get())
        except ValueError:
            return None

    def build_record(self, gross):
        dec = self.s["decimals"]
        t = self.tare()
        if gross is None or t is None:
            return None
        return {"product": self.name_var.get().strip(), "gross": core.fmt(gross, dec), "tare": core.fmt(t, dec),
                "net": core.fmt(gross - t, dec), "size": self.size_var.get().strip(), "serial": 0,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    def update_preview(self):
        g = self.gross if self.gross is not None else 0.0
        rec = self.build_record(g) or {"product": self.name_var.get(), "gross": "0.000", "tare": "0.000",
                                       "net": "0.000", "size": self.size_var.get(), "serial": 0, "ts": ""}
        img = core.render_label(rec, self.s)
        from PIL import Image
        k = 540 / img.width
        self._prev_img = ImageTk.PhotoImage(img.resize((540, int(img.height * k)), Image.LANCZOS))
        self.prev.configure(image=self._prev_img)
        t = self.tare()
        self.net_var.set("--" if (self.gross is None or t is None) else core.fmt(self.gross - t, self.s["decimals"]))

    # ------------------------------------------------------------ products
    def pick_product(self, _=None):
        t, sz = self.store.products().get(self.name_var.get(), (None, None))
        if t is not None:
            self.tare_var.set(str(t))
            self.size_var.set(sz or "")

    def save_product(self):
        n, t = self.name_var.get().strip(), self.tare()
        if not n or t is None:
            return messagebox.showwarning("Product", "Enter product name and a numeric P.Wt first.")
        self.store.save_product(n, t, self.size_var.get().strip())
        self.refresh_products()
        self.status.set(f"Saved product '{n}'")

    def delete_product(self):
        n = self.name_var.get().strip()
        if n and messagebox.askyesno("Delete", f"Delete product '{n}'?"):
            self.store.delete_product(n)
            self.refresh_products()

    # ------------------------------------------------------------ connections
    def toggle_scale(self):
        if self.scale:
            self.scale.stop.set()
            self.scale = None
            self.scale_btn.config(text="Connect")
            self.status.set("Scale disconnected")
            return
        demo = self.demo_var.get()
        if not demo and not self.scale_cb.get():
            return messagebox.showwarning("Scale", "Choose the scale's COM port (Bluetooth paired port).")
        port = "" if demo else port_name(self.scale_cb.get())
        self.s["scale_port"] = port
        core.save_settings(self.s)
        self.scale = ScaleReader(port, self.s["scale_baud"], self.q, demo)
        self.scale.start()
        self.scale_btn.config(text="Disconnect")

    def toggle_printer(self):
        if self.printer:
            self.printer.close()
            self.printer = None
            self.printer_btn.config(text="Connect")
            self.status.set("Printer disconnected")
            return
        if not self.printer_cb.get():
            return messagebox.showwarning("Printer", "Choose the printer's COM port (USB or Bluetooth).")
        try:
            self.printer = core.NiimbotPrinter(port_name(self.printer_cb.get()), self.s["printer_baud"])
        except Exception as e:
            return messagebox.showerror("Printer", f"Cannot open printer port:\n{e}")
        self.s["printer_port"] = port_name(self.printer_cb.get())
        core.save_settings(self.s)
        self.printer_btn.config(text="Disconnect")
        self.status.set("Printer connected")

    # ------------------------------------------------------------ printing
    def print_clicked(self):
        g = self.gross
        if g is None:
            return messagebox.showwarning("Print", "No weight received from the scale yet.")
        if not self.stable:
            if not messagebox.askyesno("Print", "Weight is not stable yet. Print anyway?"):
                return
        self.do_print(g)

    def do_print(self, gross, auto=False):
        t = self.tare()
        if t is None:
            return messagebox.showwarning("Print", "Enter a numeric P.Wt (kg).")
        if not self.name_var.get().strip():
            return messagebox.showwarning("Print", "Enter the product name.")
        if gross <= t:
            return messagebox.showerror("Print", "G.Wt must be greater than P.Wt - net weight would be zero or negative.")
        try:
            copies = max(1, int(self.copies_var.get()))
        except ValueError:
            copies = 1
        rec = self.build_record(gross)
        serial, ts = self.store.add_label(rec["product"], float(rec["gross"]), float(rec["tare"]),
                                          float(rec["net"]), rec["size"], copies)
        rec["serial"], rec["ts"] = serial, ts
        self.last_rec = (rec, copies)
        self.refresh_history()
        self.send_to_printer(rec, copies)
        self.last_printed_gross, self.armed = gross, False

    def send_to_printer(self, rec, copies):
        if not self.printer:
            return messagebox.showwarning("Printer", "Printer is not connected. The label was logged.\n"
                                          "Connect the printer, then press 'Reprint last'.")
        img = core.render_label(rec, self.s)
        if self.s["rotate"]:
            img = img.rotate(-self.s["rotate"], expand=True)
        w = (img.width + 7) // 8 * 8
        if w != img.width:
            from PIL import Image
            pad = Image.new("L", (w, img.height), 255)
            pad.paste(img, (0, 0))
            img = pad
        self.status.set("Printing...")

        def work():
            try:
                for _ in range(copies):
                    self.printer.print_image(img, self.s["density"])
                self.q.put(("print_ok", rec["serial"]))
            except Exception as e:
                self.q.put(("print_err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def reprint(self):
        if self.last_rec:
            self.send_to_printer(*self.last_rec)
        else:
            messagebox.showinfo("Reprint", "Nothing printed yet.")

    def test_print(self):
        rec = {"product": "TEST", "gross": "1.000", "tare": "0.100", "net": "0.900", "size": "10x10",
               "serial": 0, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.send_to_printer(rec, 1)

    def save_png(self):
        g = self.gross if self.gross is not None else 0.0
        rec = self.build_record(g)
        if not rec:
            return messagebox.showwarning("Save", "Enter numeric P.Wt first.")
        f = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if f:
            core.render_label(rec, self.s).save(f)

    def export_csv(self):
        f = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if f:
            self.store.export_csv(f)
            messagebox.showinfo("Export", "Saved.")

    # ------------------------------------------------------------ event loop
    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "scale_ok":
                    self.status.set(f"Scale connected ({val})")
                elif kind == "scale_err":
                    self.status.set(f"Scale error: {val}")
                    self.scale = None
                    self.scale_btn.config(text="Connect")
                elif kind == "scale_line":
                    self.on_scale_line(val)
                elif kind == "print_ok":
                    self.status.set(f"Printed label #{val:06d}")
                elif kind == "print_err":
                    self.status.set("Print failed")
                    messagebox.showerror("Printer", f"Printing failed:\n{val}\n\nCheck connection, then press 'Reprint last'.")
        except queue.Empty:
            pass
        self.after(80, self.poll)

    def on_scale_line(self, line):
        self.raw.set(f"Raw scale data: {line.strip()}")
        r = core.parse_scale_line(line, self.s["scale_unit"])
        if not r:
            return
        val, flag = r
        self.gross = val
        self.stable = self.stab.update(val, flag)
        self.w_var.set(core.fmt(val, self.s["decimals"]))
        self.stab_lbl.config(text="STABLE" if self.stable else "WEIGHING...", bg="#1f8f3a" if self.stable else "#d98a00")
        self.update_preview()
        # re-arm auto print once the platform is emptied or the load changes
        if not self.armed and (val < 0.01 or (self.last_printed_gross is not None
                                              and abs(val - self.last_printed_gross) > 0.02)):
            self.armed = True
        t = self.tare()
        if self.auto_var.get() and self.stable and self.armed and t is not None and val > t + 0.005:
            self.do_print(val, auto=True)

    def on_close(self):
        self.s["auto_print"] = self.auto_var.get()
        core.save_settings(self.s)
        if self.scale:
            self.scale.stop.set()
        if self.printer:
            self.printer.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
