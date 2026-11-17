import 'dart:async';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

import 'api.dart';
import 'reader_screen.dart';

void main() {
  runApp(const BookTranApp());
}

class BookTranApp extends StatelessWidget {
  const BookTranApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Book_Tran',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: const Color(0xFF5B5BD6), useMaterial3: true),
      home: const LibraryScreen(),
    );
  }
}

/// 이번 세션에서 올린 문서 목록(메모리). 영구 저장은 다음 단계.
class DocumentRef {
  DocumentRef(this.id, this.filename);
  final String id;
  final String filename;
}

class LibraryScreen extends StatefulWidget {
  const LibraryScreen({super.key});

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  final _api = ApiClient();
  final _docs = <DocumentRef>[];
  bool _uploading = false;

  Future<void> _importEpub() async {
    final picked = await FilePicker.pickFiles(
      type: FileType.custom,
      allowedExtensions: const ['epub'],
    );
    if (picked.isEmpty) return;
    final file = picked.single;
    if (file.path == null) return;

    setState(() => _uploading = true);
    try {
      final id = await _api.uploadEpub(file.path!, file.name);
      setState(() => _docs.insert(0, DocumentRef(id, file.name)));
    } catch (e) {
      _showError(e);
    } finally {
      if (mounted) setState(() => _uploading = false);
    }
  }

  void _showError(Object e) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
  }

  void _open(DocumentRef doc) {
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => DocumentScreen(api: _api, doc: doc),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Book_Tran')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _uploading ? null : _importEpub,
        icon: const Icon(Icons.file_open),
        label: Text(_uploading ? '업로드 중...' : 'EPUB 가져오기'),
      ),
      body: _docs.isEmpty
          ? const Center(child: Text('EPUB 파일을 가져와 번역을 시작하세요.'))
          : ListView.builder(
              itemCount: _docs.length,
              itemBuilder: (context, i) {
                final doc = _docs[i];
                return ListTile(
                  leading: const Icon(Icons.menu_book),
                  title: Text(doc.filename),
                  subtitle: Text(doc.id),
                  onTap: () => _open(doc),
                );
              },
            ),
    );
  }
}

enum ViewMode { original, translated, sideBySide }

class DocumentScreen extends StatefulWidget {
  const DocumentScreen({super.key, required this.api, required this.doc});

  final ApiClient api;
  final DocumentRef doc;

  @override
  State<DocumentScreen> createState() => _DocumentScreenState();
}

class _DocumentScreenState extends State<DocumentScreen> {
  List<ParagraphPair>? _rows;
  String? _jobId;
  JobStatus? _job;
  Timer? _poller;
  ViewMode _mode = ViewMode.sideBySide;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadExisting();
  }

  @override
  void dispose() {
    _poller?.cancel();
    super.dispose();
  }

  /// 이전에 번역한 결과가 있으면 다시 번역하지 않고 바로 불러온다.
  Future<void> _loadExisting() async {
    try {
      final rows = await widget.api.alignments(widget.doc.id);
      if (!mounted) return;
      setState(() {
        _rows = rows;
        _loading = false;
      });
    } catch (e) {
      _fail(e);
    }
  }

  Future<void> _startTranslation() async {
    try {
      final jobId = await widget.api.startTranslation(widget.doc.id);
      setState(() {
        _jobId = jobId;
        _job = JobStatus(status: 'queued');
      });
      _poller = Timer.periodic(const Duration(seconds: 2), (_) => _poll());
    } catch (e) {
      _fail(e);
    }
  }

  Future<void> _poll() async {
    final jobId = _jobId;
    if (jobId == null) return;
    try {
      final job = await widget.api.jobStatus(jobId);
      if (!mounted) return;
      setState(() => _job = job);
      if (job.finished) {
        _poller?.cancel();
        if (job.status == 'done') {
          final rows = await widget.api.alignments(widget.doc.id);
          if (mounted) setState(() => _rows = rows);
        }
      }
    } catch (e) {
      _poller?.cancel();
      _fail(e);
    }
  }

  void _fail(Object e) {
    if (!mounted) return;
    setState(() => _loading = false);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.doc.filename),
        actions: [
          IconButton(
            tooltip: '책으로 보기',
            icon: const Icon(Icons.auto_stories),
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(
              builder: (_) => ReaderScreen(
                api: widget.api,
                docId: widget.doc.id,
                filename: widget.doc.filename,
              ),
            )),
          ),
          if (_rows != null)
            SegmentedButton<ViewMode>(
              segments: const [
                ButtonSegment(value: ViewMode.original, label: Text('원본')),
                ButtonSegment(value: ViewMode.translated, label: Text('번역')),
                ButtonSegment(value: ViewMode.sideBySide, label: Text('나란히')),
              ],
              selected: {_mode},
              onSelectionChanged: (s) => setState(() => _mode = s.first),
            ),
          const SizedBox(width: 12),
        ],
      ),
      body: _body(),
    );
  }

  Widget _body() {
    if (_loading) return const Center(child: CircularProgressIndicator());

    final job = _job;
    final running = job != null && !job.finished;

    if (_rows == null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (running) ...[
              SizedBox(
                width: 320,
                child: LinearProgressIndicator(
                  value: (job.progressTotal ?? 0) > 0
                      ? job.progressDone! / job.progressTotal!
                      : null,
                ),
              ),
              const SizedBox(height: 12),
              Text(job.progressTotal != null
                  ? '번역 중 ${job.progressDone}/${job.progressTotal} 문단'
                  : '번역 준비 중...'),
              if (job.lastError != null) ...[
                const SizedBox(height: 8),
                Text('일부 요청 실패(원문 유지): ${job.lastError}',
                    style: const TextStyle(fontSize: 12)),
              ],
            ] else if (job?.status == 'failed') ...[
              Text('번역 실패: ${job?.error}'),
              const SizedBox(height: 12),
              FilledButton(onPressed: _startTranslation, child: const Text('다시 시도')),
            ] else ...[
              const Text('아직 번역되지 않았습니다.'),
              const SizedBox(height: 12),
              FilledButton(onPressed: _startTranslation, child: const Text('한국어로 번역')),
            ],
          ],
        ),
      );
    }

    return Column(
      children: [
        if (job?.status == 'done')
          Padding(
            padding: const EdgeInsets.all(8),
            child: Text(
              '번역 ${job!.translated}/${job.total} 문단, 원문 유지 ${job.failed}문단',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
        Expanded(child: _ParagraphView(rows: _rows!, mode: _mode)),
      ],
    );
  }
}

/// 문단 목록. 원문/번역/나란히 보기 모드를 지원한다.
/// 주의: 지금은 문단 텍스트 기준 보기이며, 실제 EPUB 조판 보기(WebView)는 다음 단계다.
class _ParagraphView extends StatelessWidget {
  const _ParagraphView({required this.rows, required this.mode});

  final List<ParagraphPair> rows;
  final ViewMode mode;

  @override
  Widget build(BuildContext context) {
    final style = Theme.of(context).textTheme.bodyLarge?.copyWith(height: 1.7);
    final muted = Theme.of(context).colorScheme.outline;

    return ListView.separated(
      padding: const EdgeInsets.all(20),
      itemCount: rows.length,
      separatorBuilder: (context, index) => const Divider(height: 24),
      itemBuilder: (context, i) {
        final row = rows[i];
        final translated = row.translated ?? row.original;
        final note = row.isTranslated ? null : '원문 유지';

        switch (mode) {
          case ViewMode.original:
            return Text(row.original, style: style);
          case ViewMode.translated:
            return _withNote(Text(translated, style: style), note, muted);
          case ViewMode.sideBySide:
            return Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(child: Text(row.original, style: style?.copyWith(color: muted))),
                const SizedBox(width: 24),
                Expanded(child: _withNote(Text(translated, style: style), note, muted)),
              ],
            );
        }
      },
    );
  }

  Widget _withNote(Widget child, String? note, Color muted) {
    if (note == null) return child;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        child,
        Text(note, style: TextStyle(fontSize: 12, color: muted)),
      ],
    );
  }
}
