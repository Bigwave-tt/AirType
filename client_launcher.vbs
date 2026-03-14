' client_launcher.vbs
' AirType クライアントPC用ランチャー。
' ホストPC（t-tak）に接続して音声入力を行います。
' このファイルを client.py と同じフォルダに置いてダブルクリックするだけで起動できます。
'
' 事前に airtype_config.json の network.server_url をホストPCのIPに設定してください。
' 例: "server_url": "http://YOUR_SERVER_IP:8000/dictate"
'
' 仮想環境の優先順位:
'   1. .\venv\Scripts\pythonw.exe   (.venv という名前)
'   2. .\venv\Scripts\pythonw.exe   (venv  という名前)
'   3. システムの pythonw.exe        (PATH から検索)
'
' ログは client.log (client.py と同じフォルダ) に出力されます。

Option Explicit

Dim fso, wsh, dir, python, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set wsh = CreateObject("WScript.Shell")

' このスクリプトと同じフォルダを作業ディレクトリにする
dir = fso.GetParentFolderName(WScript.ScriptFullName)
wsh.CurrentDirectory = dir

' 仮想環境の pythonw.exe を探す (コンソールなし起動)
If fso.FileExists(dir & "\.venv\Scripts\pythonw.exe") Then
    python = Chr(34) & dir & "\.venv\Scripts\pythonw.exe" & Chr(34)
ElseIf fso.FileExists(dir & "\venv\Scripts\pythonw.exe") Then
    python = Chr(34) & dir & "\venv\Scripts\pythonw.exe" & Chr(34)
Else
    python = "pythonw.exe"
End If

' client.py が存在するか確認
If Not fso.FileExists(dir & "\client.py") Then
    MsgBox "client.py が見つかりません:" & vbCrLf & dir & "\client.py", vbCritical, "AirType Client"
    WScript.Quit 1
End If

' バックグラウンドで起動 (第2引数 0 = ウィンドウ非表示, 第3引数 False = 非同期)
cmd = python & " " & Chr(34) & dir & "\client.py" & Chr(34)
wsh.Run cmd, 0, False

Set fso = Nothing
Set wsh = Nothing
