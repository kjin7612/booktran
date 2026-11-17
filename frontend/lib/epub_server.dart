import 'dart:io';

import 'package:mime/mime.dart';
import 'package:path/path.dart' as p;

/// WebView가 EPUB 파일을 http로 읽을 수 있게 하는 로컬 서버.
/// 127.0.0.1에만 바인딩하고, 등록된 폴더 밖의 파일은 내주지 않는다.
class LocalEpubServer {
  HttpServer? _server;
  final Map<String, Directory> _mounts = {};

  Future<void> start() async {
    if (_server != null) return;
    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _server!.listen(_handle);
  }

  /// 폴더를 /name/ 경로에 연결하고, 그 기준 URL을 돌려준다.
  String mount(String name, Directory dir) {
    final server = _server;
    if (server == null) throw StateError('서버가 시작되지 않았습니다.');
    _mounts[name] = dir;
    return 'http://127.0.0.1:${server.port}/$name/';
  }

  Future<void> stop() async {
    await _server?.close(force: true);
    _server = null;
    _mounts.clear();
  }

  Future<void> _handle(HttpRequest request) async {
    try {
      final segments = request.uri.pathSegments;
      final mounted = segments.isEmpty ? null : _mounts[segments.first];
      if (mounted == null) {
        request.response.statusCode = HttpStatus.notFound;
        return;
      }

      final relative = segments.skip(1).toList();
      final root = p.canonicalize(mounted.path);
      final file = File(p.joinAll([root, ...relative]));
      final resolved = p.canonicalize(file.path);
      if (!p.isWithin(root, resolved)) {
        request.response.statusCode = HttpStatus.forbidden;
        return;
      }
      if (!await file.exists()) {
        request.response.statusCode = HttpStatus.notFound;
        return;
      }

      final mime = lookupMimeType(resolved) ?? 'application/octet-stream';
      request.response.headers.contentType = ContentType.parse(mime);
      request.response.add(await file.readAsBytes());
    } catch (_) {
      request.response.statusCode = HttpStatus.internalServerError;
    } finally {
      await request.response.close();
    }
  }
}
