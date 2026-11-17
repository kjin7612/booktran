import 'dart:typed_data';

import 'dart:convert';

import 'package:http/http.dart' as http;

/// 백엔드(FastAPI) 클라이언트.
class ApiClient {
  ApiClient({this.baseUrl = 'http://127.0.0.1:8000', http.Client? client})
      : _client = client ?? http.Client();

  final String baseUrl;
  final http.Client _client;

  Uri _uri(String path) => Uri.parse('$baseUrl$path');

  Future<String> uploadEpub(String filePath, String filename) async {
    final request = http.MultipartRequest('POST', _uri('/documents'))
      ..files.add(await http.MultipartFile.fromPath('file', filePath, filename: filename));
    final response = await http.Response.fromStream(await _client.send(request));
    _check(response);
    return jsonDecode(response.body)['id'] as String;
  }

  Future<String> startTranslation(String docId, {String targetLang = 'ko'}) async {
    final response = await _client.post(
      _uri('/documents/$docId/translate'),
      headers: {'content-type': 'application/json'},
      body: jsonEncode({'target_lang': targetLang}),
    );
    _check(response);
    return jsonDecode(response.body)['job_id'] as String;
  }

  Future<JobStatus> jobStatus(String jobId) async {
    final response = await _client.get(_uri('/jobs/$jobId'));
    _check(response);
    return JobStatus.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  /// 번역 결과가 있으면 문단 대응표를, 없으면 null을 돌려준다.
  Future<List<ParagraphPair>?> alignments(String docId, {String lang = 'ko'}) async {
    final response = await _client.get(_uri('/documents/$docId/alignments?lang=$lang'));
    if (response.statusCode == 404) return null;
    _check(response);
    final list = jsonDecode(response.body) as List<dynamic>;
    return list.map((e) => ParagraphPair.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<Uint8List> downloadOriginal(String docId) async {
    final response = await _client.get(_uri('/documents/$docId/original'));
    _check(response);
    return response.bodyBytes;
  }

  /// 번역본이 아직 없으면 null을 돌려준다.
  Future<Uint8List?> downloadTranslated(String docId, {String lang = 'ko'}) async {
    final response = await _client.get(_uri('/documents/$docId/translated?lang=$lang'));
    if (response.statusCode == 404) return null;
    _check(response);
    return response.bodyBytes;
  }

  void _check(http.Response response) {
    if (response.statusCode >= 400) {
      throw ApiException(response.statusCode, response.body);
    }
  }
}

class ApiException implements Exception {
  ApiException(this.statusCode, this.body);
  final int statusCode;
  final String body;

  @override
  String toString() => 'API 오류 $statusCode: $body';
}

class JobStatus {
  JobStatus({
    required this.status,
    this.error,
    this.translated,
    this.total,
    this.failed,
    this.progressDone,
    this.progressTotal,
    this.lastError,
  });

  final String status; // queued | running | done | failed
  final String? error;
  final int? translated;
  final int? total;
  final int? failed;
  final int? progressDone;
  final int? progressTotal;

  /// 재시도 끝에 실패한 최근 요청의 오류(번역은 계속 진행된다).
  final String? lastError;

  bool get finished => status == 'done' || status == 'failed';

  factory JobStatus.fromJson(Map<String, dynamic> json) {
    final progress = json['progress'] as Map<String, dynamic>?;
    return JobStatus(
      status: json['status'] as String,
      error: json['error'] as String?,
      translated: json['translated'] as int?,
      total: json['total'] as int?,
      failed: json['failed'] as int?,
      progressDone: progress?['done'] as int?,
      progressTotal: progress?['total'] as int?,
      lastError: json['last_error'] as String?,
    );
  }
}

/// 문단 하나의 원문과 번역문. translated가 null이면 원문이 유지된 문단이다.
class ParagraphPair {
  ParagraphPair({
    required this.doc,
    required this.index,
    required this.original,
    required this.translated,
    required this.status,
  });

  final String doc;
  final int index;
  final String original;
  final String? translated;
  final String status; // translated | failed | original

  bool get isTranslated => status == 'translated';

  factory ParagraphPair.fromJson(Map<String, dynamic> json) => ParagraphPair(
        doc: json['doc'] as String,
        index: json['index'] as int,
        original: json['original'] as String,
        translated: json['translated'] as String?,
        status: json['status'] as String,
      );
}
