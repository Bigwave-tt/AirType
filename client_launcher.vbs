' client_launcher.vbs
' AirType クライアントPC用ランチャー。
' ホストPC（t-tak）に接続して音声入力を行います。
' このファイルを client.py と同じフォルダに置いてダブルクリックするだけで起動できます。
'
' 事前に airtype_config.json の network.server_url をホストPCのIPに設定してください。
' 例: "server_url": "http://YOUR_SERVER_IP:8000/dictate"
'
' 仮想環境の優先順位:
'   1. .\.venv\Scripts\python.exe   (.venv という名前)
'   2. .\venv\Scripts\python.exe    (venv  という名前)
'   3. システムの python.exe         (PATH から検索)
'
' client.py はコンソール出力のみのため、コンソールウィンドウを表示して起動します。
' ウィンドウを閉じるとアプリが終了します。

Option Explicit

Dim fso, wsh, dir, python, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set wsh = CreateObject("WScript.Shell")

' このスクリプトと同じフォルダを作業ディレクトリにする
dir = fso.GetParentFolderName(WScript.ScriptFullName)
wsh.CurrentDirectory = dir

' 仮想環境の python.exe を探す (コンソールあり起動)
If fso.FileExists(dir & "\.venv\Scripts\python.exe") Then
    python = Chr(34) & dir & "\.venv\Scripts\python.exe" & Chr(34)
ElseIf fso.FileExists(dir & "\venv\Scripts\python.exe") Then
    python = Chr(34) & dir & "\venv\Scripts\python.exe" & Chr(34)
Else
    python = "python.exe"
End If

' client.py が存在するか確認
If Not fso.FileExists(dir & "\client.py") Then
    MsgBox "client.py が見つかりません:" & vbCrLf & dir & "\client.py", vbCritical, "AirType Client"
    WScript.Quit 1
End If

' コンソールウィンドウあり・非同期で起動 (第2引数 1 = 通常ウィンドウ表示)
cmd = python & " " & Chr(34) & dir & "\client.py" & Chr(34)
wsh.Run cmd, 1, False

Set fso = Nothing
Set wsh = Nothing
