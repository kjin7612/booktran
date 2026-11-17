import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:webview_windows/webview_windows.dart';

import 'api.dart';
import 'epub_book.dart';
import 'epub_server.dart';

enum ReaderMode { original, translated, sideBySide }

/// 원본과 번역본을 실제 EPUB 조판 그대로 WebView에 띄우는 리더.
/// 나란히 보기에서는 스크롤 위치를 비율로 맞춘다(같은 장 기준).
class ReaderScreen extends StatefulWidget {
  const ReaderScreen({super.key, required this.api, required this.docId, required this.filename});

  final ApiClient api;
  final String docId;
  final String filename;

  @override
  State<ReaderScreen> createState() => _ReaderScreenState();
}

/// 각 WebView에 주입되는 스크롤 동기화 스크립트.
/// 스크롤 비율을 Dart로 보내고(__btSetRatio로 받은 비율을 적용한다).
const _syncScript = r'''
(function () {
  function maxScroll() {
    return Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
  }
  window.__btSetRatio = function (r) {
    window.scrollTo(0, r * maxScroll());
  };
  var timer = null;
  window.addEventListener('scroll', function () {
    if (timer) return;
    timer = setTimeout(function () {
      timer = null;
      window.chrome.webview.postMessage(JSON.stringify({
        type: 'scroll',
        ratio: window.scrollY / maxScroll()
      }));
    }, 60);
  }, { passive: true });
})();
''';

class _ReaderScreenState extends State<ReaderScreen> {
  final _server = LocalEpubServer();
  final _origCtl = WebviewController();
  final _transCtl = WebviewController();

  Directory? _booksDir;
  EpubBook? _orig;
  EpubBook? _trans;
  String? _origBase;
  String? _transBase;

  ReaderMode _mode = ReaderMode.sideBySide;
  int _chapter = 0;
  double _ratio = 0;
  bool _ready = false;
  String? _error;

  // 프로그램이 스크롤을 옮긴 직후 생기는 이벤트를 무시해 동기화 루프를 막는다.
  final _lastProgrammatic = <WebviewController, DateTime>{};

  @override
  void initState() {
    super.initState();
    _init();
  }

  @override
  void dispose() {
    _savePosition();
    _server.stop();
    _origCtl.dispose();
    _transCtl.dispose();
    super.dispose();
  }

  bool get _hasTranslation => _trans != null;

  Future<void> _init() async {
    try {
      final support = await getApplicationSupportDirectory();
      _booksDir = Directory(p.join(support.path, 'books', widget.docId));

      _orig = await _loadBook('original', () => widget.api.downloadOriginal(widget.docId));
      _trans = await _loadBook('translated', () => widget.api.downloadTranslated(widget.docId));

      await _server.start();
      _origBase = _server.mount('orig', _orig!.rootDir);
      if (_trans != null) _transBase = _server.mount('trans', _trans!.rootDir);

      await _initController(_origCtl);
      if (_hasTranslation) await _initController(_transCtl);

      await _restorePosition();
      if (!mounted) return;
      setState(() => _ready = true);
      await _showChapter(_chapter, ratio: _ratio);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  /// 캐시된 폴더가 있으면 그대로 쓰고, 없으면 내려받아 푼다. 번역본이 없으면 null.
  Future<EpubBook?> _loadBook(String name, Future<Uint8List?> Function() fetch) async {
    final target = Directory(p.join(_booksDir!.path, name));
    if (await File(p.join(target.path, 'META-INF', 'container.xml')).exists()) {
      return EpubBook.open(target);
    }

    final bytes = await fetch();
    if (bytes == null) return null;

    // 임시 폴더에 다 푼 뒤 이름을 바꿔, 중간에 실패해도 불완전한 캐시가 남지 않게 한다.
    final tmp = Directory('${target.path}.tmp');
    if (await tmp.exists()) await tmp.delete(recursive: true);
    await tmp.create(recursive: true);
    await extractEpub(bytes, tmp);
    if (await target.exists()) await target.delete(recursive: true);
    await tmp.rename(target.path);
    return EpubBook.open(target);
  }

  Future<void> _initController(WebviewController c) async {
    await c.initialize();
    await c.addScriptToExecuteOnDocumentCreated(_syncScript);
    c.webMessage.listen((raw) => _onScroll(c, raw));
    c.loadingState.listen((state) {
      if (state == LoadingState.navigationCompleted) _applyRatio(c);
    });
  }

  void _onScroll(WebviewController source, dynamic raw) {
    final data = raw is String ? jsonDecode(raw) : raw;
    if (data is! Map || data['type'] != 'scroll') return;
    final ratio = (data['ratio'] as num).toDouble();

    final last = _lastProgrammatic[source];
    if (last != null && DateTime.now().difference(last) < const Duration(milliseconds: 400)) {
      return;
    }

    _ratio = ratio;
    if (_mode == ReaderMode.sideBySide && _hasTranslation) {
      final other = identical(source, _origCtl) ? _transCtl : _origCtl;
      _setRatio(other, ratio);
    }
  }

  void _setRatio(WebviewController c, double ratio) {
    _lastProgrammatic[c] = DateTime.now();
    c.executeScript('window.__btSetRatio($ratio)');
  }

  void _applyRatio(WebviewController c) {
    if (_ratio > 0) _setRatio(c, _ratio);
  }

  String _urlFor(String base, EpubBook book, int index) {
    final path = book.spine[index].split('/').map(Uri.encodeComponent).join('/');
    return '$base$path';
  }

  int get _chapterCount => _orig?.spine.length ?? 0;

  Future<void> _showChapter(int index, {double ratio = 0}) async {
    if (_orig == null) return;
    final i = index.clamp(0, _chapterCount - 1);
    _chapter = i;
    _ratio = ratio;

    await _origCtl.loadUrl(_urlFor(_origBase!, _orig!, i));
    if (_hasTranslation && i < _trans!.spine.length) {
      await _transCtl.loadUrl(_urlFor(_transBase!, _trans!, i));
    }
    await _savePosition();
    if (mounted) setState(() {});
  }

  Future<void> _savePosition() async {
    final dir = _booksDir;
    if (dir == null) return;
    await dir.create(recursive: true);
    await File(p.join(dir.path, 'position.json'))
        .writeAsString(jsonEncode({'chapter': _chapter, 'ratio': _ratio}));
  }

  Future<void> _restorePosition() async {
    final file = File(p.join(_booksDir!.path, 'position.json'));
    if (!await file.exists()) return;
    final data = jsonDecode(await file.readAsString()) as Map<String, dynamic>;
    _chapter = (data['chapter'] as int).clamp(0, _chapterCount - 1);
    _ratio = (data['ratio'] as num).toDouble();
  }

  void _changeMode(ReaderMode mode) {
    setState(() => _mode = mode);
    // 보이는 쪽으로 현재 위치를 맞춘다.
    if (mode != ReaderMode.original && _hasTranslation) _setRatio(_transCtl, _ratio);
    if (mode != ReaderMode.translated) _setRatio(_origCtl, _ratio);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_orig?.title.isNotEmpty == true ? _orig!.title : widget.filename),
        actions: [
          if (_ready)
            SegmentedButton<ReaderMode>(
              segments: const [
                ButtonSegment(value: ReaderMode.original, label: Text('원본')),
                ButtonSegment(value: ReaderMode.translated, label: Text('번역')),
                ButtonSegment(value: ReaderMode.sideBySide, label: Text('나란히')),
              ],
              selected: {_mode},
              onSelectionChanged: (s) => _changeMode(s.first),
            ),
          const SizedBox(width: 12),
        ],
      ),
      body: _body(),
    );
  }

  Widget _body() {
    if (_error != null) {
      return Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(_error!)));
    }
    if (!_ready) return const Center(child: CircularProgressIndicator());

    return Column(
      children: [
        _chapterBar(),
        const Divider(height: 1),
        Expanded(child: _panes()),
      ],
    );
  }

  Widget _chapterBar() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Row(
        children: [
          IconButton(
            tooltip: '이전 장',
            onPressed: _chapter > 0 ? () => _showChapter(_chapter - 1) : null,
            icon: const Icon(Icons.chevron_left),
          ),
          Text('${_chapter + 1} / $_chapterCount장'),
          IconButton(
            tooltip: '다음 장',
            onPressed: _chapter < _chapterCount - 1 ? () => _showChapter(_chapter + 1) : null,
            icon: const Icon(Icons.chevron_right),
          ),
          if (!_hasTranslation) ...[
            const SizedBox(width: 16),
            Text(
              '번역본이 없습니다. 목록 화면에서 번역을 먼저 실행하세요.',
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
        ],
      ),
    );
  }

  Widget _panes() {
    switch (_mode) {
      case ReaderMode.original:
        return Webview(_origCtl);
      case ReaderMode.translated:
        return _hasTranslation ? Webview(_transCtl) : const SizedBox.shrink();
      case ReaderMode.sideBySide:
        return Row(
          children: [
            Expanded(child: Webview(_origCtl)),
            const VerticalDivider(width: 1),
            Expanded(
              child: _hasTranslation ? Webview(_transCtl) : const Center(child: Text('번역본 없음')),
            ),
          ],
        );
    }
  }
}
