import 'package:book_tran/api.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('ParagraphPair parses backend alignment JSON', () {
    final pair = ParagraphPair.fromJson({
      'doc': 'OEBPS/Text/ch1.xhtml',
      'index': 3,
      'original': 'Hello world',
      'translated': '[KO] Hello world',
      'status': 'translated',
    });
    expect(pair.isTranslated, isTrue);
    expect(pair.translated, '[KO] Hello world');
  });

  test('ParagraphPair keeps original when translation failed', () {
    final pair = ParagraphPair.fromJson({
      'doc': 'OEBPS/Text/ch1.xhtml',
      'index': 0,
      'original': 'Kept as is',
      'translated': null,
      'status': 'failed',
    });
    expect(pair.isTranslated, isFalse);
    expect(pair.translated, isNull);
  });

  test('JobStatus reports finished states', () {
    expect(JobStatus.fromJson({'status': 'running'}).finished, isFalse);
    expect(JobStatus.fromJson({'status': 'done', 'total': 4, 'translated': 4, 'failed': 0}).finished, isTrue);
    expect(JobStatus.fromJson({'status': 'failed', 'error': 'boom'}).error, 'boom');
  });
}
