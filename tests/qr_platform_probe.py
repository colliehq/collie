import json,sys,tempfile,subprocess
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import test_qr

original_run=subprocess.run
scan=[]
def logged_run(*args,**kwargs):
    result=original_run(*args,**kwargs)
    print('COMMAND',args[0], 'EXIT',result.returncode,'STDOUT',repr(result.stdout),'STDERR',repr(result.stderr),flush=True)
    if len(args[0])>3 and args[0][1]=='swift': scan.append(args[0])
    return result
with patch.object(subprocess,'run',logged_run):
    test_qr.test_vision_decodes_the_png()
if not scan: raise SystemExit('no scan')
folder=Path(scan[-1][2]).parent
png=Path(scan[-1][3])
Image.open(png).convert('RGB').save(folder/'rgb.png')
probe=folder/'probe.swift'
probe.write_text('''import Foundation
import Vision
import ImageIO
let url = URL(fileURLWithPath: CommandLine.arguments[1])
for revision in VNDetectBarcodesRequest.supportedRevisions {
 for cpu in [false, true] {
  let request = VNDetectBarcodesRequest()
  request.revision = revision
  request.symbologies = [.qr]
  request.usesCPUOnly = cpu
  do {
   try VNImageRequestHandler(url: url, options: [:]).perform([request])
   print("revision=\\(revision) cpu=\\(cpu) codes=\\((request.results ?? []).compactMap { $0.payloadStringValue })")
  } catch { print("revision=\\(revision) cpu=\\(cpu) error=\\(error)") }
 }
}
''',encoding='utf8')
for file in [png,folder/'rgb.png']:
    logged_run(['xcrun','swift',str(probe),str(file)],capture_output=True,text=True,timeout=300)
