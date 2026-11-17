import 'dart:io';
import 'dart:typed_data';

import 'package:archive/archive.dart';
import 'package:path/path.dart' as p;
import 'package:xml/xml.dart';

/// 풀어 놓은 EPUB 폴더와 읽기 순서(spine)를 담는다.
class EpubBook {
  EpubBook({required this.rootDir, required this.title, required this.spine});

  /// EPUB zip 내용을 풀어 둔 폴더.
  final Directory rootDir;
  final String title;

  /// rootDir 기준 posix 상대 경로 목록(읽기 순서).
  final List<String> spine;

  static Future<EpubBook> open(Directory rootDir) async {
    final container = XmlDocument.parse(
      await File(p.join(rootDir.path, 'META-INF', 'container.xml')).readAsString(),
    );
    final rootfile = container.descendants
        .whereType<XmlElement>()
        .firstWhere((e) => e.name.local == 'rootfile');
    final opfPath = rootfile.getAttribute('full-path')!;
    final opfDir = p.posix.dirname(opfPath);

    final opf = XmlDocument.parse(
      await File(p.join(rootDir.path, opfPath)).readAsString(),
    );
    final elements = opf.descendants.whereType<XmlElement>();

    final title = elements
            .where((e) => e.name.local == 'title')
            .map((e) => e.innerText.trim())
            .firstOrNull ??
        '';

    final manifest = <String, String>{};
    for (final item in elements.where((e) => e.name.local == 'item')) {
      final id = item.getAttribute('id');
      final href = item.getAttribute('href');
      if (id != null && href != null) manifest[id] = href;
    }

    final spine = <String>[];
    for (final ref in elements.where((e) => e.name.local == 'itemref')) {
      final href = manifest[ref.getAttribute('idref')];
      if (href == null) continue;
      spine.add(p.posix.normalize(p.posix.join(opfDir, Uri.decodeFull(href))));
    }

    return EpubBook(rootDir: rootDir, title: title, spine: spine);
  }
}

/// EPUB zip 바이트를 target 폴더에 푼다. 경로 탈출(zip slip)은 막는다.
Future<void> extractEpub(Uint8List bytes, Directory target) async {
  final archive = ZipDecoder().decodeBytes(bytes);
  final root = p.canonicalize(target.path);
  for (final entry in archive) {
    if (!entry.isFile) continue;
    final out = File(p.join(root, entry.name));
    if (!p.isWithin(root, p.canonicalize(out.path))) {
      throw FormatException('EPUB 경로가 올바르지 않습니다: ${entry.name}');
    }
    await out.create(recursive: true);
    await out.writeAsBytes(entry.content as List<int>);
  }
}
