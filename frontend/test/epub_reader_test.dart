import 'dart:io';
import 'dart:typed_data';

import 'package:archive/archive.dart';
import 'package:book_tran/epub_book.dart';
import 'package:book_tran/epub_server.dart';
import 'package:flutter_test/flutter_test.dart';

const _opf = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title></metadata>
  <manifest>
    <item id="b" href="Text/second%20chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="a" href="Text/first.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="a"/><itemref idref="b"/></spine>
</package>''';

const _container = '''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''';

ArchiveFile _file(String name, String text) {
  final bytes = text.codeUnits;
  return ArchiveFile(name, bytes.length, bytes);
}

Uint8List _makeEpub() {
  final archive = Archive()
    ..addFile(_file('mimetype', 'application/epub+zip'))
    ..addFile(_file('META-INF/container.xml', _container))
    ..addFile(_file('OEBPS/content.opf', _opf))
    ..addFile(_file('OEBPS/Text/first.xhtml', 'first'))
    ..addFile(_file('OEBPS/Text/second chapter.xhtml', 'second'));
  return Uint8List.fromList(ZipEncoder().encode(archive));
}

void main() {
  late Directory tmp;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('book_tran_test');
  });

  tearDown(() async {
    await tmp.delete(recursive: true);
  });

  test('opens an extracted EPUB and reads title and spine order', () async {
    final root = Directory('${tmp.path}/book');
    await extractEpub(_makeEpub(), root);

    final book = await EpubBook.open(root);
    expect(book.title, 'Test Book');
    expect(book.spine, ['OEBPS/Text/first.xhtml', 'OEBPS/Text/second chapter.xhtml']);
  });

  test('serves files from a mounted folder and refuses paths outside it', () async {
    final root = Directory('${tmp.path}/book');
    await extractEpub(_makeEpub(), root);
    File('${tmp.path}/secret.txt').writeAsStringSync('nope');

    final server = LocalEpubServer();
    await server.start();
    addTearDown(server.stop);
    final base = server.mount('orig', root);

    final client = HttpClient();
    addTearDown(client.close);

    Future<int> status(String url) async {
      final req = await client.getUrl(Uri.parse(url));
      final res = await req.close();
      await res.drain<void>();
      return res.statusCode;
    }

    expect(await status('${base}OEBPS/Text/first.xhtml'), 200);
    expect(await status('${base}OEBPS/Text/missing.xhtml'), 404);
    expect(await status('$base../secret.txt'), isNot(200));
  });
}
