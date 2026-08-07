; Collie — one-click Windows installer (Inno Setup 6+).
;
; Produces Collie-Setup.exe: a non-technical user double-clicks it and ends up with a "Collie" icon
; that opens a real desktop window. No Python, no terminal, no pip, no PATH surgery — the exact
; friction that made `pip install collie-harness` a dead end for beginners ("collie: command not
; found", because the Scripts dir isn't on PATH).
;
; Everything ships inside: an embeddable CPython with collie + its semantic-memory deps already
; installed, the wallpaper engine's C# source + WebView2 DLLs (the .exe is compiled on first use by
; the in-box csc, so no .NET SDK is needed), the browser extension, and the WebView2 bootstrapper.
;
; The wizard opens on a branded star-map welcome page, then a custom card-style language picker
; (77 languages, Simplified Chinese up front — see gen_langs.py for why a custom page replaced
; Inno's alphabetical native dialog). Whatever you pick becomes Collie's own UI language on the very
; first launch, so nothing needs configuring afterward.
;
; BUILD (see installer/README.md for staging the payload):
;   python installer\make_art.py     ->  installer\art\*.bmp      (branding, reproducible)
;   python installer\gen_langs.py    ->  installer\languages.iss + langdata.iss
;   iscc installer\collie.iss        ->  installer\Output\Collie-Setup.exe

#define AppName    "Collie"
; Version comes from harness/__init__.py, passed at build time: iscc /DAppVer=x.y.z (build.ps1 /
; the release workflow read it from the package). The fallback keeps a bare `iscc collie.iss` working.
#ifndef AppVer
  #define AppVer   "0.0.0-dev"
#endif
#define Publisher  "Collie"
#define AppUrl     "https://github.com/wudaming/collie"
#define PyW        "{app}\python\pythonw.exe"
#define IcoFile    "{app}\python\Lib\site-packages\harness\wallpaper\collie.ico"

[Setup]
AppId={{B7A41C58-9F2E-4D3A-8E11-C0111E5A77D2}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#Publisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}
AppComments=A memory-first coding agent that verifies its own work.
; per-user install: no admin prompt, and it matches the per-user logon autostart collie registers
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Collie
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Collie-Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; ---- look and feel -------------------------------------------------------------------------
; The stock wizard reads like a 2003 shareware installer. The branded star-map panel is collie's
; own identity (same motif as the live wallpaper), generated from the logo by installer/make_art.py.
; Inno picks the entry from each ladder that matches the user's DPI — hence the sizes.
WizardStyle=modern
; a touch larger than the default so it reads like an app window, not a cramped setup dialog
WizardSizePercent=135
SetupIconFile=..\harness\wallpaper\collie.ico
WizardImageFile=art\wizard-164x314.bmp,art\wizard-192x386.bmp,art\wizard-256x492.bmp,art\wizard-328x628.bmp,art\wizard-355x700.bmp,art\wizard-410x797.bmp
WizardSmallImageFile=art\wizard-small-55x58.bmp,art\wizard-small-64x68.bmp,art\wizard-small-92x97.bmp,art\wizard-small-110x116.bmp,art\wizard-small-119x123.bmp,art\wizard-small-138x140.bmp
; keep the branded welcome page; suppress Inno's native combo dialog — our custom card page replaces it
DisableWelcomePage=no
ShowLanguageDialog=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={#IcoFile}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoVersion={#AppVer}

[Languages]
#include "languages.iss"

[CustomMessages]
; The language-page chrome. Translated for the languages collie's GUI also speaks; every other
; wizard language falls back to the bare English line.
LangTitle=Language
LangSub=Pick the language for Collie and for the rest of Setup.
LangMore=More languages
LangHint=You can change this any time in Collie's settings.
StatusWebView2=Installing the WebView2 runtime...
StatusLang=Applying your language...
StatusWallpaper=Setting up the desktop wallpaper...
StatusBridge=Setting up the browser bridge...
TaskWallpaper=Live star-map wallpaper on my desktop
TaskBridge=Let collie use my real browser (already logged in)
RunApp=Start Collie now
zh.LangTitle=语言
zh.LangSub=选择 Collie 与安装向导使用的语言。
zh.LangMore=更多语言
zh.LangHint=之后随时可以在 Collie 的设置里更改。
zh.StatusWebView2=正在安装 WebView2 运行时...
zh.StatusLang=正在应用你选择的语言...
zh.StatusWallpaper=正在设置桌面壁纸...
zh.StatusBridge=正在设置浏览器桥接...
zh.TaskWallpaper=把实时星图设为桌面壁纸
zh.TaskBridge=允许 collie 使用我已登录的真实浏览器
zh.RunApp=立即启动 Collie
zhtw.LangTitle=語言
zhtw.LangSub=選擇 Collie 與安裝精靈使用的語言。
zhtw.LangMore=更多語言
zhtw.LangHint=之後隨時可以在 Collie 的設定裡變更。
zhtw.StatusWebView2=正在安裝 WebView2 執行階段...
zhtw.StatusLang=正在套用你選擇的語言...
zhtw.StatusWallpaper=正在設定桌面桌布...
zhtw.StatusBridge=正在設定瀏覽器橋接...
zhtw.TaskWallpaper=把即時星圖設為桌面桌布
zhtw.TaskBridge=允許 collie 使用我已登入的真實瀏覽器
zhtw.RunApp=立即啟動 Collie
ja.LangTitle=言語
ja.LangSub=Collie とインストーラーで使う言語を選んでください。
ja.LangMore=その他の言語
ja.LangHint=Collie の設定でいつでも変更できます。
ja.RunApp=Collie を今すぐ起動
es.LangTitle=Idioma
es.LangSub=Elige el idioma de Collie y del resto del instalador.
es.LangMore=Más idiomas
es.LangHint=Puedes cambiarlo cuando quieras en los ajustes de Collie.
es.RunApp=Iniciar Collie ahora
fr.LangTitle=Langue
fr.LangSub=Choisissez la langue de Collie et du reste de l'installation.
fr.LangMore=Plus de langues
fr.LangHint=Vous pourrez la changer à tout moment dans les réglages de Collie.
fr.RunApp=Lancer Collie maintenant
de.LangTitle=Sprache
de.LangSub=Wählen Sie die Sprache für Collie und den Rest des Setups.
de.LangMore=Weitere Sprachen
de.LangHint=Sie können sie jederzeit in den Collie-Einstellungen ändern.
de.RunApp=Collie jetzt starten

[Messages]
; "Setup - Collie" in the title bar reads like an installer; just "Collie" reads like an app.
SetupWindowTitle=%1
; The stock welcome/finish text says nothing about what you just downloaded. Overridden for the
; two primary audiences; every other language keeps Inno's translated default.
en.WelcomeLabel2=Collie is a coding agent that remembers your project and verifies its own work before it calls anything done.%n%nEverything it needs ships inside this installer — no Python, no terminal, no configuration. Just click Next.
en.FinishedLabel=Collie is installed. Open it from the Start menu (or the desktop icon) and pick a brain on first launch — an existing Claude, Codex, or Grok subscription connects in one click.
zh.WelcomeLabel2=Collie 是一个会记住你项目的编程 agent——它会先自己跑起来验证,通过了才说「做完了」。%n%n运行所需的一切都已经打包在这个安装程序里:不需要 Python、不需要命令行、不需要任何配置,点「下一步」就行。
zh.FinishedLabel=Collie 已安装完成。从开始菜单(或桌面图标)打开它,首次启动时选一个「大脑」——已有的 Claude、Codex 或 Grok 订阅可以一键接入。
zhtw.WelcomeLabel2=Collie 是一個會記住你專案的編程 agent——它會先自己跑起來驗證,通過了才說「做完了」。%n%n執行所需的一切都已經打包在這個安裝程式裡:不需要 Python、不需要命令列、不需要任何設定,按「下一步」就行。
zhtw.FinishedLabel=Collie 已安裝完成。從開始功能表(或桌面圖示)開啟它,首次啟動時選一個「大腦」——已有的 Claude、Codex 或 Grok 訂閱可以一鍵接入。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "wallpaper";   Description: "{cm:TaskWallpaper}"; Flags: unchecked
Name: "bridge";      Description: "{cm:TaskBridge}"; Flags: unchecked

[Files]
; the self-contained runtime: embeddable CPython + collie + deps + engine source + extension
Source: "payload\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
; tiny bootstrapper; installs the WebView2 runtime only if the machine lacks it
Source: "payload\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
; the full-bleed welcome splash — extracted to {tmp} and painted over the whole welcome page ([Code])
Source: "art\welcome-hero-900x570.bmp"; Flags: dontcopy

[Icons]
Name: "{group}\{#AppName}";        Filename: "{#PyW}"; Parameters: "-m harness.cli app"; WorkingDir: "{app}\python"; IconFilename: "{#IcoFile}"
Name: "{autodesktop}\{#AppName}";  Filename: "{#PyW}"; Parameters: "-m harness.cli app"; WorkingDir: "{app}\python"; IconFilename: "{#IcoFile}"; Tasks: desktopicon
Name: "{group}\Collie Settings";   Filename: "{#PyW}"; Parameters: "-m harness.cli setup"; WorkingDir: "{app}\python"; IconFilename: "{#IcoFile}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; 1) WebView2 runtime (silent, no-op when already present) — the desktop window needs it
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "{cm:StatusWebView2}"; Flags: waituntilterminated

; 2) carry the chosen language into the app, so the first launch is already localized.
;    {code:AppLangParam} expands to `config LANG <code>` (or a harmless no-op when it's "auto").
Filename: "{#PyW}"; Parameters: "{code:AppLangParam}"; WorkingDir: "{app}\python"; \
  StatusMsg: "{cm:StatusLang}"; Flags: runhidden waituntilterminated

; 3) optional: the live-wallpaper desktop, and the real-browser bridge, at logon
Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper --install"; WorkingDir: "{app}\python"; \
  StatusMsg: "{cm:StatusWallpaper}"; Flags: runhidden waituntilterminated; Tasks: wallpaper
Filename: "{#PyW}"; Parameters: "-m harness.cli browser-bridge --install"; WorkingDir: "{app}\python"; \
  StatusMsg: "{cm:StatusBridge}"; Flags: runhidden waituntilterminated; Tasks: bridge

; 4) launch it
;    A previous release's updater does not know Slack listeners exist. The NEW
;    installer therefore owns this migration: after replacing the runtime,
;    restart every per-dog launcher the user opted into. A duplicate loses the
;    per-dog OS lock and exits harmlessly.
Filename: "{#PyW}"; \
  Parameters: "-c ""import glob,os,subprocess,sys; [subprocess.Popen([sys.executable,p], creationflags=0x08000000) for p in glob.glob(os.path.expanduser('~/.collie/slack-*.pyw'))]"""; \
  StatusMsg: "Restarting Slack agents..."; Flags: runhidden waituntilterminated

; 5) launch the app
Filename: "{#PyW}"; Parameters: "-m harness.cli app"; WorkingDir: "{app}\python"; \
  Description: "{cm:RunApp}"; Flags: runhidden postinstall nowait skipifsilent

[UninstallRun]
; Stop what's running from the install dir BEFORE the files disappear — FAST (taskkill + one short
; powershell), NOT by cold-starting the embeddable python three times to run harness commands: each
; of those loads collie's heavy deps (onnx/providers), so the old approach made uninstall look hung
; for ~40s. The logon autostart .vbs files are removed by [UninstallDelete] below (instant, no python).
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM collie-wallpaper.exe"; \
  RunOnceId: "KillWallpaper"; Flags: runhidden waituntilterminated
Filename: "{cmd}"; \
  Parameters: "/C powershell -NoProfile -Command ""Get-Process python,pythonw -EA SilentlyContinue | Where-Object {{ $_.Path -like '{app}\*' }} | Stop-Process -Force"""; \
  RunOnceId: "KillAppPython"; Flags: runhidden waituntilterminated

[UninstallDelete]
; the logon autostart launchers (Startup folder) — removed directly so we don't need to run python
Type: files; Name: "{userstartup}\collie-wallpaper.vbs"
Type: files; Name: "{userstartup}\collie-bridge.vbs"
; Inno only removes what it INSTALLED. The app generates files afterward that it can't track — the
; engine .exe compiled on first run from the shipped C# source, __pycache__ (.pyc), and any runtime
; data written under {app}. Without this, uninstall leaves ~180 MB of the bundled runtime behind.
; Runs after [UninstallRun], so the wallpaper is stopped and the autostarts are gone first. User
; data in ~/.collie (settings, memory) is intentionally NOT touched — a reinstall keeps it.
Type: filesandordirs; Name: "{app}"

[Code]
const
  COLS = 4; CHIP_H = 44; GAP = 10;
  { Dark theme — matches the WebView2 shell (installer.html) so the wizard fallback reads as the same
    product. TColor is BGR, so each literal is the #RRGGBB reversed. }
  C_ACCENT = $F16663;   { #6366F1 — the shell's indigo }
  C_CHIP   = $2B2622;   { #22262B — dark glass chip }
  C_TEXT   = $FAF5F4;   { #F4F5FA — light ink }
  C_MUTED  = $B6A39D;   { #9DA3B6 — muted light }
  C_LINE   = $332A26;   { #262A33 — hairline divider on dark }
  C_DARK   = $1D1614;   { #14161D — form background }
  C_BTN    = $2E2824;   { neutral dark button (Cancel) }
  { DwmSetWindowAttribute IDs (Win11 22000+; older Windows ignore them harmlessly) }
  DWMWA_WINDOW_CORNER_PREFERENCE = 33;
  DWMWCP_ROUND = 2;

function DwmSetWindowAttribute(hwnd: HWND; attr: Integer; var value: DWORD; cb: Integer): Integer;
  external 'DwmSetWindowAttribute@dwmapi.dll stdcall';
function SetTimer(hwnd: HWND; id, elapse, cb: LongWord): LongWord; external 'SetTimer@user32.dll stdcall';
function KillTimer(hwnd: HWND; id: LongWord): Boolean; external 'KillTimer@user32.dll stdcall';

{ Keep the rounded-corner + soft-shadow look after we strip the system border (bsNone otherwise gives
  a flat rectangle). No-op before Win11 — safe to always call. }
procedure RoundCorners;
var v: DWORD;
begin
  try
    v := DWMWCP_ROUND; DwmSetWindowAttribute(WizardForm.Handle, DWMWA_WINDOW_CORNER_PREFERENCE, v, 4);
  except
  end;
end;

{ Drive the real Inno buttons (kept, but hidden behind the full-window hero) from our custom card
  controls via BM_CLICK, so all the wizard's navigation + validation still runs — we replace only
  the look, not the behaviour. (Reading .OnClick directly is a type error in Pascal Script.) }
procedure BtnNextClick(Sender: TObject);
begin
  SendMessage(WizardForm.NextButton.Handle, $00F5, 0, 0);   { BM_CLICK }
end;
procedure BtnCancelClick(Sender: TObject);
begin
  SendMessage(WizardForm.CancelButton.Handle, $00F5, 0, 0);
end;

function StripAmp(s: String): String;
var i: Integer;
begin
  Result := '';
  for i := 1 to Length(s) do if s[i] <> '&' then Result := Result + s[i];
end;

procedure StyleBtn(p: TPanel; accent: Boolean);
begin
  p.BevelOuter := bvNone; p.ParentBackground := False;
  p.Font.Size := 10; p.Cursor := crHand;
  if accent then begin p.Color := C_ACCENT; p.Font.Color := clWhite; p.Font.Style := [fsBold]; end
  else begin p.Color := C_BTN; p.Font.Color := $C8CEDA; p.Font.Style := []; end;
end;

var
  LangPage: TWizardPage;
  Chips: array of TPanel;
  ChipCode, MoreCode: TStringList;
  MoreBox: TNewComboBox;
  Sel: Integer;          { chip index, or -1 when the "more" combo owns the selection }
  AppLang: String;       { Collie UI-language code chosen on the language page }
  Relaunching: Boolean;
  HeroImg: TBitmapImage; { full-window splash on the welcome page (borderless, no system chrome) }
  ChipTop: Integer;      { y of the first chip row — lets the grid sit lower, not jammed at the top }
  BtnNext, BtnCancel, BtnClose: TPanel;   { custom themed nav fused onto the card, replacing the
                                            system-chrome title bar + OS buttons }
  BottomBar: TPanel;     { opaque dark strip that hides the real OS button row on the welcome card }
  OrigFormColor: TColor; HaveOrigColor: Boolean;   { restore the light form bg off the welcome page }
  TimerCb: LongWord;     { WinAPI-timer callback that re-hides Inno's buttons after it re-shows them }
  CurPage: Integer;      { the page currently shown (the timer callback reads it) }

procedure Repaint;
var i: Integer;
begin
  for i := 0 to GetArrayLength(Chips) - 1 do begin
    if i = Sel then begin
      Chips[i].Color := C_ACCENT; Chips[i].Font.Color := clWhite; Chips[i].Font.Style := [fsBold];
    end else begin
      Chips[i].Color := C_CHIP; Chips[i].Font.Color := C_TEXT; Chips[i].Font.Style := [];
    end;
  end;
end;

function Chosen: String;
begin
  if Sel >= 0 then Result := ChipCode[Sel]
  else if MoreBox.ItemIndex >= 0 then Result := MoreCode[MoreBox.ItemIndex]
  else Result := 'en';
end;

procedure ChipClick(Sender: TObject);
begin
  Sel := TPanel(Sender).Tag;
  MoreBox.ItemIndex := -1;
  Repaint;
end;

procedure MoreChange(Sender: TObject);
begin
  if MoreBox.ItemIndex >= 0 then begin Sel := -1; Repaint; end;
end;

procedure AddChip(const Native, English, Code: String);
var i, row, col, w: Integer; p: TPanel;
begin
  i := GetArrayLength(Chips); SetArrayLength(Chips, i + 1);
  row := i div COLS; col := i mod COLS;
  w := (LangPage.SurfaceWidth - (COLS - 1) * ScaleX(GAP)) div COLS;
  p := TPanel.Create(LangPage);
  p.Parent := LangPage.Surface;
  p.SetBounds(col * (w + ScaleX(GAP)), ChipTop + row * (ScaleY(CHIP_H) + ScaleY(GAP)), w, ScaleY(CHIP_H));
  p.BevelOuter := bvNone;
  p.ParentBackground := False;
  p.Caption := Native;
  p.Font.Size := 10;
  p.Cursor := crHand;
  p.Tag := i;
  p.OnClick := @ChipClick;
  Chips[i] := p;
  ChipCode.Add(Code);
end;

procedure AddMore(const Native, English, Code: String);
begin
  MoreBox.Items.Add(Native + '   ·   ' + English);
  MoreCode.Add(Code);
end;

{ langdata.iss defines BuildLanguageList (the AddChip/AddMore calls) and CollieLang(code); it must
  come after AddChip/AddMore are declared and before BuildLanguageList/CollieLang are first used. }
#include "langdata.iss"

procedure PreselectCurrent;
var i: Integer;
begin
  Sel := -1;
  for i := 0 to ChipCode.Count - 1 do
    if CompareText(ChipCode[i], ExpandConstant('{language}')) = 0 then Sel := i;
  if Sel < 0 then
    for i := 0 to MoreCode.Count - 1 do
      if CompareText(MoreCode[i], ExpandConstant('{language}')) = 0 then MoreBox.ItemIndex := i;
  if (Sel < 0) and (MoreBox.ItemIndex < 0) then Sel := 0;   { default to English chip }
  Repaint;
end;

{ Before copying any files, stop a PREVIOUS install's collie that's running OUT OF the install dir —
  the live wallpaper engine, the desktop app window, the browser bridge. They hold python.dll/*.pyd
  open, so an upgrade/reinstall would fail the copy with an "Abort/Retry/Ignore" that
  /SUPPRESSMSGBOXES turns into exit code 5. We kill only processes whose path is under the install
  dir, so an unrelated Python elsewhere is never touched. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var rc: Integer; app: String;
begin
  Result := '';
  app := ExpandConstant('{app}');
  if DirExists(app) then begin
    { let it shut the wallpaper down cleanly first (best-effort) }
    try Exec(app + '\python\pythonw.exe', '-m harness.cli wallpaper --stop', '',
             SW_HIDE, ewWaitUntilTerminated, rc); except end;
    { Then stop each exact app-runtime tree. Stop-Process killed only the
      python parent; a legacy Slack task's shell/node/git descendants could
      continue editing during the upgrade and overlap the replacement worker. }
    Exec(ExpandConstant('{cmd}'),
         '/C powershell -NoProfile -Command "Get-Process python,pythonw,collie-wallpaper '
         + '-ErrorAction SilentlyContinue | Where-Object { $_.Path -like ''' + app + '\*'' } '
         + '| ForEach-Object { & taskkill.exe /PID $_.Id /T /F | Out-Null }"',
         '', SW_HIDE, ewWaitUntilTerminated, rc);
    Sleep(700);
  end;
end;

procedure InitializeWizard;
var y: Integer; lbl: TNewStaticText; divider: TPanel;
begin
  { In silent mode there is no wizard to build (the smart shell drives us with /VERYSILENT). All the
    UI setup below touches WizardForm, which errors when there is no visible wizard — so skip it, or
    the whole silent install aborts with exit code 1. The language Run step derives the UI language
    from the active wizard language via CollieLang, so it needs nothing from here. }
  if WizardSilent() then Exit;

  ChipCode := TStringList.Create; MoreCode := TStringList.Create;
  LangPage := CreateCustomPage(wpWelcome, ExpandConstant('{cm:LangTitle}'),
                               ExpandConstant('{cm:LangSub}'));
  try LangPage.Surface.Color := C_DARK; except end;   { dark surface behind the language cards }

  { chips are laid out immediately; the combo must exist before AddMore is called }
  MoreBox := TNewComboBox.Create(LangPage);
  MoreBox.Parent := LangPage.Surface;
  MoreBox.Style := csDropDownList;
  MoreBox.Color := C_CHIP; MoreBox.Font.Color := C_TEXT;   { dark dropdown, not a white native combo }
  MoreBox.OnChange := @MoreChange;

  ChipTop := ScaleY(10);   { let the grid breathe below the header, not jammed to the top edge }
  BuildLanguageList;       { generated: AddChip x12 then AddMore }

  y := ChipTop + ((GetArrayLength(Chips) + COLS - 1) div COLS) * (ScaleY(CHIP_H) + ScaleY(GAP))
       + ScaleY(16);

  { hairline divider separates the common languages from the long tail }
  divider := TPanel.Create(LangPage);
  divider.Parent := LangPage.Surface;
  divider.SetBounds(0, y, LangPage.SurfaceWidth, 1);
  divider.BevelOuter := bvNone; divider.ParentBackground := False; divider.Color := C_LINE;
  y := y + ScaleY(16);

  lbl := TNewStaticText.Create(LangPage);
  lbl.Parent := LangPage.Surface;
  lbl.SetBounds(0, y, LangPage.SurfaceWidth, ScaleY(15));
  lbl.Font.Color := C_MUTED;
  lbl.Caption := ExpandConstant('{cm:LangMore}');

  MoreBox.SetBounds(0, y + ScaleY(20), LangPage.SurfaceWidth, ScaleY(22));   { full width, not a stub }

  { a bottom-anchored brand line fills what used to be dead space and ties the page to the app }
  lbl := TNewStaticText.Create(LangPage);
  lbl.Parent := LangPage.Surface;
  lbl.SetBounds(0, LangPage.SurfaceHeight - ScaleY(18), LangPage.SurfaceWidth, ScaleY(15));
  lbl.Font.Color := C_MUTED;
  lbl.Caption := ExpandConstant('{cm:LangHint}');

  PreselectCurrent;

  { --- borderless, chrome-free window: strip the system title bar + frame entirely --- }
  WizardForm.BorderStyle := bsNone;
  RoundCorners;

  { the welcome splash fills the welcome PAGE (parented to it so it sits above the notebook); the
    bottom button strip below the page is darkened separately in CurPageChanged so the two meet
    seamlessly (the hero's bottom is the same near-black as the strip). }
  ExtractTemporaryFile('welcome-hero-900x570.bmp');
  HeroImg := TBitmapImage.Create(WizardForm);
  HeroImg.Parent := WizardForm.WelcomePage;
  HeroImg.Bitmap.LoadFromFile(ExpandConstant('{tmp}\welcome-hero-900x570.bmp'));
  HeroImg.Stretch := True;
  HeroImg.Visible := False;

  { custom nav fused onto the card — the real Inno buttons are hidden and these drive them }
  BtnNext := TPanel.Create(WizardForm);   BtnNext.Parent := WizardForm;   StyleBtn(BtnNext, True);
  BtnNext.OnClick := @BtnNextClick;        BtnNext.Visible := False;
  BtnCancel := TPanel.Create(WizardForm); BtnCancel.Parent := WizardForm; StyleBtn(BtnCancel, False);
  BtnCancel.OnClick := @BtnCancelClick;    BtnCancel.Visible := False;
  { the close affordance the missing title bar would have provided }
  BtnClose := TPanel.Create(WizardForm);  BtnClose.Parent := WizardForm;
  BtnClose.BevelOuter := bvNone; BtnClose.ParentBackground := False; BtnClose.Color := C_DARK;
  BtnClose.Font.Color := $9AA0B0; BtnClose.Font.Size := 12; BtnClose.Caption := 'X';
  BtnClose.Cursor := crHand; BtnClose.OnClick := @BtnCancelClick; BtnClose.Visible := False;
  { opaque dark strip that covers the real OS button row (which stays live for BM_CLICK) }
  BottomBar := TPanel.Create(WizardForm); BottomBar.Parent := WizardForm;
  BottomBar.BevelOuter := bvNone; BottomBar.ParentBackground := False; BottomBar.Color := C_DARK;
  BottomBar.Visible := False;
end;

{ Inno re-shows Next/Cancel AFTER CurPageChanged returns, so hiding them there doesn't stick. This
  fires ~40ms later, once the page has settled, and re-hides them + lifts our themed nav on top. }
procedure RehideButtons(H: HWND; Msg, IdEvent, Time: LongWord);
begin
  KillTimer(0, IdEvent);
  if (HeroImg <> nil) and (CurPage = wpWelcome) then begin
    WizardForm.NextButton.Visible := False;
    WizardForm.CancelButton.Visible := False;
    WizardForm.BackButton.Visible := False;
    BottomBar.BringToFront;
    BtnNext.BringToFront; BtnCancel.BringToFront; BtnClose.BringToFront;
  end;
end;

{ Recolor the stock inner pages (Tasks / Ready / Installing / Finished) to the dark theme so they match
  the welcome hero and the WebView2 shell. Each assignment is guarded — a control absent on a given
  page or Inno build must not abort the wizard. }
procedure ApplyDark;
begin
  WizardForm.Color := C_DARK;
  try WizardForm.MainPanel.Color := C_DARK; except end;
  try WizardForm.PageNameLabel.Font.Color := C_TEXT; except end;
  try WizardForm.PageDescriptionLabel.Font.Color := C_MUTED; except end;
  try WizardForm.Bevel.Visible := False; except end;         { hairline under the header }
  try WizardForm.Bevel1.Visible := False; except end;        { hairline above the buttons }
  try WizardForm.InnerPage.Color := C_DARK; except end;
  try WizardForm.TasksList.Color := C_DARK; WizardForm.TasksList.Font.Color := C_TEXT; except end;
  try WizardForm.ReadyMemo.Color := C_DARK; WizardForm.ReadyMemo.Font.Color := C_TEXT; except end;
  { the stock pages' body copy is near-black by default — unreadable on dark, so lift each to muted light }
  try WizardForm.SelectDirLabel.Font.Color := C_MUTED; except end;
  try WizardForm.SelectDirBrowseLabel.Font.Color := C_MUTED; except end;
  try WizardForm.DiskSpaceLabel.Font.Color := C_MUTED; except end;
  try WizardForm.SelectTasksLabel.Font.Color := C_MUTED; except end;
  try WizardForm.SelectStartMenuFolderLabel.Font.Color := C_MUTED; except end;
  try WizardForm.ReadyLabel.Font.Color := C_MUTED; except end;
  try WizardForm.DirEdit.Color := C_CHIP; WizardForm.DirEdit.Font.Color := C_TEXT; except end;
  try WizardForm.SelectDirBitmapImage.Visible := False; except end;
  try WizardForm.StatusLabel.Font.Color := C_MUTED; except end;
  try WizardForm.FilenameLabel.Font.Color := C_MUTED; except end;
  try WizardForm.FinishedHeadingLabel.Font.Color := C_TEXT; except end;
  try WizardForm.FinishedLabel.Font.Color := C_TEXT; except end;
end;

procedure CurPageChanged(CurPageID: Integer);
var welcome: Boolean; cw, cs: Integer; nb, cb: TNewButton;
begin
  RoundCorners;   { re-assert after the handle is fully realized on the first page show }
  if HeroImg = nil then exit;
  CurPage := CurPageID;   { remember for the re-hide timer callback }
  if not HaveOrigColor then begin OrigFormColor := WizardForm.Color; HaveOrigColor := True; end;
  welcome := (CurPageID = wpWelcome);
  cw := WizardForm.ClientWidth;
  nb := WizardForm.NextButton; cb := WizardForm.CancelButton;

  { hero fills the welcome page; other pages show their normal content }
  HeroImg.Visible := welcome;
  WizardForm.WizardBitmapImage.Visible := not welcome;   { Finished page reuses this — keep it }
  WizardForm.WelcomeLabel1.Visible := not welcome;
  WizardForm.WelcomeLabel2.Visible := not welcome;
  { hide the real OS buttons on the card (they wouldn't stay behind our panels — Inno keeps them on
    top); they remain functional via BM_CLICK even while hidden. Restored on inner pages. }
  WizardForm.NextButton.Visible := not welcome;
  WizardForm.CancelButton.Visible := not welcome;
  BtnNext.Visible := welcome; BtnCancel.Visible := welcome;

  { close 'X' (top-right) replaces the missing title-bar button — on every page }
  cs := ScaleY(30);
  BtnClose.SetBounds(cw - cs, 0, cs, cs);
  BtnClose.Visible := True; BtnClose.BringToFront;

  if welcome then begin
    { dark the strip below the page so it merges with the hero's near-black bottom }
    WizardForm.Color := C_DARK;
    HeroImg.SetBounds(0, 0, WizardForm.WelcomePage.ClientWidth, WizardForm.WelcomePage.ClientHeight);
    HeroImg.BringToFront;
    { opaque dark bar over the whole OS button row, then our themed buttons on top of it — the real
      buttons stay behind it, live, driven by BM_CLICK }
    BottomBar.SetBounds(0, nb.Top - ScaleY(14), cw, WizardForm.ClientHeight - (nb.Top - ScaleY(14)));
    BottomBar.Visible := True; BottomBar.BringToFront;
    BtnNext.Caption := StripAmp(nb.Caption);
    BtnCancel.Caption := StripAmp(cb.Caption);
    BtnNext.SetBounds(nb.Left, nb.Top, nb.Width, nb.Height);
    BtnCancel.SetBounds(cb.Left, cb.Top, cb.Width, cb.Height);
    BtnNext.BringToFront; BtnCancel.BringToFront; BtnClose.BringToFront;
    if TimerCb = 0 then TimerCb := CreateCallback(@RehideButtons);
    SetTimer(0, 0, 40, TimerCb);   { re-hide the OS buttons after Inno re-shows them }
  end else begin
    ApplyDark;                                          { dark inner pages, matching the hero + shell }
    WizardForm.WizardSmallBitmapImage.Visible := False; { the dog logo shared the top-right corner with
                                                          our close 'X' — hide it so they never overlap }
    BottomBar.Visible := False;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var rc: Integer; pick: String;
begin
  Result := True;
  { Pascal Script `and` does NOT short-circuit — in silent mode LangPage is nil, so a combined
    `(LangPage <> nil) and (CurPageID = LangPage.ID)` would still deref LangPage.ID and EAbort the
    whole (silent) install. Guard with nested ifs. }
  if LangPage = nil then Exit;
  if CurPageID = LangPage.ID then begin
    pick := Chosen;
    AppLang := CollieLang(pick);   { record for the [Run] step regardless of what happens next }
    { Relaunch Setup in the chosen language so the wizard chrome matches too. If the OS blocks a
      self-relaunch (locked-down / controlled-folder machines return access-denied), we DON'T trap
      the user on this page — we just proceed in the current chrome; the app language is already
      captured above, which is what actually matters. }
    if CompareText(pick, ExpandConstant('{language}')) <> 0 then begin
      Relaunching := True;
      if Exec(ExpandConstant('{srcexe}'), '/LANG=' + pick, '', SW_SHOW, ewNoWait, rc) then begin
        Result := False;
        PostMessage(WizardForm.Handle, $0010, 0, 0);   { WM_CLOSE the old instance }
      end else
        Relaunching := False;                          { relaunch blocked — carry on, no error }
    end;
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if Relaunching then Confirm := False;   { the "are you sure you want to cancel?" is not our close }
end;

{ Expands the language Run line. Derive Collie's UI language from the ACTIVE wizard language (the
  language constant, which /LANG= sets) rather than the card page's AppLang var — so it works in
  silent mode too (the smart-shell drives the backend with /VERYSILENT /LANG=xx, and the card page
  never runs). "auto" => follow the browser, so run a harmless version query instead of writing. }
function AppLangParam(Param: String): String;
var c: String;
begin
  c := CollieLang(ExpandConstant('{language}'));
  if (c = '') or (CompareText(c, 'auto') = 0) then
    Result := '-m harness.cli config'
  else
    Result := '-m harness.cli config LANG ' + c;
end;
